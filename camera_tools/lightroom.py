"""File-based Lightroom Classic bridge, with no GUI or Lightroom dependencies.

Each invocation reads request.json from an existing job directory and atomically
writes response.json. Preview saves the exact source snapshots in preview.json;
copy reads that snapshot rather than scanning the card again. Only Python reads
preview.json, preserving nanosecond timestamps and inode numbers as integers.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import time
from datetime import date, datetime
from pathlib import Path
from threading import Event
from typing import Any

from .core import ImportOptions, MediaItem, ScanResult, discover_sources, import_media, scan_media


PROTOCOL = 1
PROGRESS_INTERVAL = 0.2


def _atomic_json(path: Path, value: dict) -> None:
    """Readers see either the previous complete document or the next one."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _object(pairs: list[tuple[str, Any]]) -> dict:
    value: dict = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON field: {key}")
        value[key] = item
    return value


def _invalid_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON number: {value}")


def _read_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle, object_pairs_hook=_object, parse_constant=_invalid_constant)
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object.")
    if type(value.get("protocol")) is not int or value["protocol"] != PROTOCOL:
        raise ValueError(f"Unsupported {path.name} protocol; expected {PROTOCOL}.")
    return value


def _text(value: Any, name: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or "\0" in value or (not empty and not value.strip()):
        raise ValueError(f"{name} must be {'a' if empty else 'a non-empty'} text value.")
    return value


def _integer(value: Any, name: str, *, minimum: int | None = 0) -> int:
    if type(value) is not int or (minimum is not None and value < minimum):
        raise ValueError(f"{name} must be an integer" + (f" of at least {minimum}." if minimum is not None else "."))
    return value


def _boolean(value: Any, name: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{name} must be true or false.")
    return value


def _cutoff(value: Any) -> date | None:
    if value is None or value == "":
        return None
    value = _text(value, "cutoff")
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError:
        raise ValueError("cutoff must be a calendar date in YYYY-MM-DD format.") from None


def _options(value: dict) -> ImportOptions:
    source = Path(_text(value.get("source"), "source"))
    destination = Path(_text(value.get("destination"), "destination"))
    basis = _text(value.get("date_basis", "capture"), "date_basis")
    if basis not in {"capture", "modified"}:
        raise ValueError("date_basis must be 'capture' or 'modified'.")
    return ImportOptions(
        source=source, destination=destination, cutoff=_cutoff(value.get("cutoff")),
        include_videos=_boolean(value.get("include_videos", True), "include_videos"),
        date_basis=basis,
    )


def _absolute_path(value: Any, name: str) -> Path:
    path = Path(_text(value, name))
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{name} must be an absolute path without traversal components.")
    # Do not resolve: a new symlink must not replace the preview's canonical path.
    return path


def _save_preview(path: Path, scan: ScanResult) -> None:
    options = scan.options
    _atomic_json(path, {
        "protocol": PROTOCOL,
        "options": {
            "source": str(options.source), "destination": str(options.destination),
            "cutoff": options.cutoff.isoformat() if options.cutoff else None,
            "include_videos": options.include_videos, "date_basis": options.date_basis,
        },
        "items": [{
            "source": str(item.source), "relative_destination": str(item.relative_destination),
            "size": item.size, "taken_at": item.taken_at.isoformat(),
            "date_source": item.date_source, "kind": item.kind,
            "mtime_ns": item.mtime_ns, "group_id": item.group_id,
            "device": item.device, "inode": item.inode,
        } for item in scan.items],
        "filtered": scan.filtered, "warnings": scan.warnings,
        "fallback_count": scan.fallback_count, "cancelled": scan.cancelled,
    })


def _load_preview(path: Path) -> ScanResult:
    try:
        value = _read_json(path)
    except FileNotFoundError:
        raise ValueError("No completed preview is available. Preview the import before copying.") from None
    options_value = value.get("options")
    if not isinstance(options_value, dict):
        raise ValueError("The saved preview has invalid options; preview the import again.")
    options = _options(options_value)
    _absolute_path(options_value.get("source"), "Preview source")
    _absolute_path(options_value.get("destination"), "Preview destination")
    if _boolean(value.get("cancelled"), "Preview cancelled"):
        raise ValueError("The preview was cancelled. Preview the import again before copying.")
    rows = value.get("items")
    if not isinstance(rows, list):
        raise ValueError("The saved preview must contain an items list.")
    items = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Each saved preview item must be an object.")
        source = _absolute_path(row.get("source"), "Preview item source")
        if not source.is_relative_to(options.source):
            raise ValueError("The saved preview contains a file outside the source folder.")
        relative = Path(_text(row.get("relative_destination"), "Preview destination path"))
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 4:
            raise ValueError("The saved preview contains an unsafe destination path.")
        kind = _text(row.get("kind"), "Preview kind")
        if kind not in {"raw", "photo", "video", "sidecar"}:
            raise ValueError("The saved preview contains an invalid media kind.")
        date_source = _text(row.get("date_source"), "Preview date source")
        if date_source not in {"capture", "modified"}:
            raise ValueError("The saved preview contains an invalid date source.")
        try:
            taken_at = datetime.fromisoformat(_text(row.get("taken_at"), "Preview date"))
        except ValueError:
            raise ValueError("The saved preview contains an invalid photo date.") from None
        if taken_at.tzinfo is not None:
            raise ValueError("The saved preview photo date must use camera wall-clock time.")
        items.append(MediaItem(
            source=source, relative_destination=relative,
            size=_integer(row.get("size"), "Preview size"), taken_at=taken_at,
            date_source=date_source, kind=kind,
            mtime_ns=_integer(row.get("mtime_ns"), "Preview modification time", minimum=None),
            group_id=_text(row.get("group_id"), "Preview group", empty=True),
            device=_integer(row.get("device"), "Preview device"),
            inode=_integer(row.get("inode"), "Preview inode"),
        ))
    warnings = value.get("warnings")
    if not isinstance(warnings, list) or not all(isinstance(item, str) for item in warnings):
        raise ValueError("The saved preview contains invalid warnings.")
    return ScanResult(
        options=options, items=items,
        filtered=_integer(value.get("filtered"), "Preview filtered count"),
        warnings=warnings,
        fallback_count=_integer(value.get("fallback_count"), "Preview fallback count"),
    )


class _FileCancellation(Event):
    """Check at most twenty times per second and latch the cancellation signal."""

    def __init__(self, path: Path):
        super().__init__()
        self.path = path
        self.next_check = 0.0

    def is_set(self) -> bool:
        if super().is_set():
            return True
        now = time.monotonic()
        if now >= self.next_check:
            self.next_check = now + 0.05
            if self.path.exists():
                self.set()
        return super().is_set()


class _Progress:
    def __init__(self, path: Path, phase: str):
        self.path = path
        self.phase = phase
        self.last_write = 0.0
        self.completed = 0

    def report(self, completed: int, total: int, message: str, *, force: bool = False) -> None:
        self.completed = completed
        now = time.monotonic()
        if force or now - self.last_write >= PROGRESS_INTERVAL:
            _atomic_json(self.path, {
                "protocol": PROTOCOL, "phase": self.phase,
                "message": message, "completed": completed, "total": total,
            })
            self.last_write = now


def _preview_text(scan: ScanResult) -> str:
    lines = ["Filename | Type | Date | Date source | Size | Destination (relative)"]
    for item in scan.items:
        # JSON-style quoting preserves newlines and tabs in unusual filenames.
        filename = json.dumps(item.source.name, ensure_ascii=False)
        destination = json.dumps(str(item.relative_destination), ensure_ascii=False)
        lines.append(
            f"{filename} | {item.kind.upper()} | {item.taken_at.isoformat(sep=' ')} | "
            f"{item.date_source} | {item.size:,} bytes | {destination}"
        )
    if not scan.items:
        lines.append("No matching files.")
    return "\n".join(lines)


def run_job(job: Path) -> dict:
    request = _read_json(job / "request.json")
    command = request.get("command")
    response: dict = {"protocol": PROTOCOL, "status": "ok"}
    if command == "discover":
        response["sources"] = [{"label": source.label, "path": str(source.path)} for source in discover_sources()]
        return response
    if command not in {"preview", "copy"}:
        raise ValueError("command must be 'discover', 'preview', or 'copy'.")
    cancel = _FileCancellation(job / "cancel")
    progress = _Progress(job / "progress.json", "scan" if command == "preview" else "copy")
    if command == "preview":
        # An unsuccessful replacement preview must never leave an old one usable.
        (job / "preview.json").unlink(missing_ok=True)
        options = _options(request)
        progress.report(0, 0, "Scanning source", force=True)
        scan = scan_media(options, cancel, lambda count, message: progress.report(count, 0, message))
        _save_preview(job / "preview.json", scan)
        count = len(scan.items)
        progress.report(count, count, "Preview cancelled" if scan.cancelled else "Preview complete", force=True)
        response.update(
            source=str(scan.options.source), destination=str(scan.options.destination),
            count=count, total_bytes=scan.total_bytes, filtered=scan.filtered,
            fallback_count=scan.fallback_count, warnings=scan.warnings,
            cancelled=scan.cancelled, preview_text=_preview_text(scan),
        )
    else:
        scan = _load_preview(job / "preview.json")
        progress.report(0, scan.total_bytes, "Checking preview and destination", force=True)
        result = import_media(scan, cancel, progress.report)
        files = []
        seen: set[Path] = set()
        for item in result.files:
            if item.kind != "sidecar" and item.path not in seen:
                files.append({"path": str(item.path), "kind": item.kind})
                seen.add(item.path)
        response.update(
            copied=result.copied, skipped=result.skipped, renamed=result.renamed,
            cancelled=result.cancelled, errors=result.errors, files=files,
        )
        # Keep accurate partial progress on cancellation or failures.
        message = "Copy cancelled" if result.cancelled else "Copy finished"
        if not result.cancelled and not result.errors:
            progress.report(scan.total_bytes, scan.total_bytes, message, force=True)
        else:
            progress.report(progress.completed, scan.total_bytes, message, force=True)
    return response


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a Lightroom Classic photo importer job.")
    parser.add_argument("--job", required=True, help="Absolute path to the existing job directory")
    arguments = parser.parse_args(argv)
    try:
        job = _absolute_path(arguments.job, "Job directory")
        if not job.is_dir():
            raise ValueError(f"Job directory is unavailable: {job}")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    try:
        response = run_job(job)
    except (OSError, ValueError, TypeError, OverflowError) as exc:
        response = {"protocol": PROTOCOL, "status": "error", "error": str(exc)}
    try:
        _atomic_json(job / "response.json", response)
    except (OSError, ValueError) as exc:
        print(f"Cannot write helper response: {exc}", file=sys.stderr)
        return 1
    return 0 if response["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
