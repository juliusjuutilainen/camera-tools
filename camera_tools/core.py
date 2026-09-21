"""Scan mounted cards and copy media into YYYY/MM/DD without overwriting files.

Scanning never reads entire files or writes to the library. Capture dates are camera
wall-clock dates, so timezone conversion cannot move a photograph to another day.
RAW/JPEG pairs and their sidecars share a date and any collision suffix.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
import uuid
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
from threading import Event
from typing import Callable


RAW_EXTENSIONS = frozenset({
    ".raw", ".raf", ".arw", ".crw", ".rw2", ".orf", ".nef", ".nrw",
    ".pef", ".dng", ".cr2", ".cr3", ".srw", ".rwl", ".3fr", ".fff",
    ".iiq", ".mos", ".mrw", ".sr2", ".srf", ".x3f",
})
PHOTO_EXTENSIONS = RAW_EXTENSIONS | frozenset({
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif", ".psd",
    ".heic", ".heif", ".hif", ".avif", ".webp",
})
VIDEO_EXTENSIONS = frozenset({
    ".mp4", ".avi", ".mov", ".mkv", ".flv", ".wmv", ".m4v", ".mts",
    ".m2ts", ".mpg", ".mpeg", ".3gp", ".mxf",
})
SIDECAR_EXTENSIONS = frozenset({".xmp", ".aae"})
COPY_CHUNK_SIZE = 4 * 1024 * 1024


@dataclass(frozen=True)
class Source:
    label: str
    path: Path


@dataclass(frozen=True)
class ImportOptions:
    source: Path
    destination: Path
    cutoff: date | None = None
    include_videos: bool = True
    date_basis: str = "capture"


@dataclass(frozen=True)
class MediaItem:
    source: Path
    relative_destination: Path
    size: int
    taken_at: datetime
    date_source: str
    kind: str
    mtime_ns: int | None = None
    group_id: str = ""
    device: int | None = field(default=None, repr=False)
    inode: int | None = field(default=None, repr=False)


@dataclass
class ScanResult:
    options: ImportOptions
    items: list[MediaItem]
    filtered: int
    warnings: list[str]
    fallback_count: int
    cancelled: bool = False

    @property
    def total_bytes(self) -> int:
        return sum(item.size for item in self.items)


@dataclass
class ImportResult:
    copied: int = 0
    skipped: int = 0
    renamed: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    bytes_copied: int = 0
    files: list[ImportedFile] = field(default_factory=list)


@dataclass(frozen=True)
class ImportedFile:
    """An actual destination that was copied or verified as an identical file."""

    path: Path
    kind: str


@dataclass(frozen=True)
class _Candidate:
    path: Path
    info: os.stat_result
    kind: str


@dataclass(frozen=True)
class _PlannedItem:
    item: MediaItem
    relative_destination: Path


class _Cancelled(Exception):
    pass


def _check_cancel(cancel: Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise _Cancelled


def _volume_candidates() -> list[Path]:
    if sys.platform == "win32":
        return [Path(f"{letter}:/") for letter in "DEFGHIJKLMNOPQRSTUVWXYZ"]
    if sys.platform == "darwin":
        roots = [Path("/Volumes")]
    else:
        username = os.environ.get("USER", "")
        roots = [Path("/media"), Path("/mnt")]
        if username:
            roots.extend([Path("/media") / username, Path("/run/media") / username])
    candidates: list[Path] = []
    for root in roots:
        try:
            candidates.extend(root.iterdir())
        except OSError:
            continue
    return candidates


def discover_sources() -> list[Source]:
    """Find mounted volumes with camera folders; PTP-only cameras aren't volumes."""
    sources: list[Source] = []
    seen: set[Path] = set()
    for candidate in _volume_candidates():
        try:
            if candidate.is_symlink() or not candidate.is_dir():
                continue
            resolved = candidate.resolve()
            if resolved in seen or os.path.samefile(resolved, Path("/")):
                continue
            with os.scandir(candidate) as entries:
                is_camera = any(
                    entry.name.upper() in {"DCIM", "PRIVATE", "AVCHD"}
                    and entry.is_dir(follow_symlinks=False)
                    for entry in entries
                )
            if is_camera:
                sources.append(Source(candidate.name or str(candidate), resolved))
                seen.add(resolved)
        except OSError:
            continue
    return sorted(sources, key=lambda source: (source.label.casefold(), str(source.path)))


def _validated_options(options: ImportOptions) -> ImportOptions:
    if not str(options.source).strip() or not str(options.destination).strip():
        raise ValueError("Choose both a source folder and a destination folder.")
    source = Path(options.source).expanduser().resolve()
    destination = Path(options.destination).expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"The source folder is unavailable: {source}")
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Source and destination must be separate, non-overlapping folders.")
    if destination.exists() and not destination.is_dir():
        raise ValueError("The destination must be a folder.")
    if options.date_basis not in {"capture", "modified"}:
        raise ValueError("Date basis must be 'capture' or 'modified'.")
    if options.cutoff is not None and (
        not isinstance(options.cutoff, date) or isinstance(options.cutoff, datetime)
    ):
        raise ValueError("The cutoff must be a calendar date.")
    return replace(options, source=source, destination=destination)


def _base_stem(path: Path) -> str:
    stem = path.stem
    if path.suffix.lower() in SIDECAR_EXTENSIONS:
        inner = Path(stem)
        if inner.suffix.lower() in PHOTO_EXTENSIONS | VIDEO_EXTENSIONS:
            stem = inner.stem
    return stem


def _kind(path: Path) -> str | None:
    suffix = path.suffix.lower()
    if suffix in RAW_EXTENSIONS:
        return "raw"
    if suffix in PHOTO_EXTENSIONS:
        return "photo"
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in SIDECAR_EXTENSIONS:
        return "sidecar"
    return None


def _read_capture_datetime(path: Path) -> datetime | None:
    # Optional here so the engine and modification-date mode also work on stdlib.
    import exifread

    with path.open("rb") as handle:
        tags = exifread.process_file(
            handle, details=False, extract_thumbnail=False, stop_tag="DateTimeOriginal",
        )
    tag = tags.get("EXIF DateTimeOriginal") or tags.get("Image DateTimeOriginal")
    if tag is None:
        return None
    try:
        return datetime.strptime(str(tag).strip()[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


def scan_media(
    options: ImportOptions,
    cancel: Event | None = None,
    progress: Callable[[int, str], None] | None = None,
) -> ScanResult:
    """Build a read-only preview; cutoff is inclusive and applies to whole pairs."""
    options = _validated_options(options)
    result = ScanResult(options, [], 0, [], 0)
    groups: dict[tuple[Path, str], list[_Candidate]] = defaultdict(list)
    found = 0
    metadata_available = True
    try:
        pending = [options.source]
        while pending:
            _check_cancel(cancel)
            directory = pending.pop()
            try:
                with os.scandir(directory) as entries:
                    children = sorted(entries, key=lambda entry: (entry.name.casefold(), entry.name))
            except OSError as exc:
                result.warnings.append(f"Cannot read {directory}: {exc}")
                continue
            subdirectories = []
            for entry in children:
                _check_cancel(cancel)
                if entry.name.startswith(".") or entry.is_symlink():
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        subdirectories.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    path = Path(entry.path)
                    kind = _kind(path)
                    if kind is None:
                        continue
                    if kind == "video" and not options.include_videos:
                        result.filtered += 1
                        continue
                    info = entry.stat(follow_symlinks=False)
                    groups[(path.parent, _base_stem(path).casefold())].append(
                        _Candidate(path, info, kind)
                    )
                    found += 1
                    if progress:
                        progress(found, f"Found {path.name}")
                except OSError as exc:
                    result.warnings.append(f"Cannot read {entry.path}: {exc}")
            pending.extend(reversed(subdirectories))

        for (parent, stem), candidates in groups.items():
            _check_cancel(cancel)
            media = [candidate for candidate in candidates if candidate.kind != "sidecar"]
            if not media:
                # Don't import orphaned metadata or sidecars of excluded videos.
                result.filtered += len(candidates)
                continue
            names = [candidate.path.name.casefold() for candidate in candidates]
            if len(set(names)) != len(names):
                result.warnings.append(
                    f"Case-only filename differences in {parent}: conflicting files "
                    "will get separate suffixes; review RAW/JPEG pairing."
                )
            taken_at = None
            date_source = "modified"
            if options.date_basis == "capture" and metadata_available:
                ranked = sorted(media, key=lambda candidate: (
                    0 if candidate.path.suffix.lower() in {".jpg", ".jpeg"}
                    else 1 if candidate.kind == "raw" else 2,
                    candidate.path.name.casefold(), candidate.path.name,
                ))
                for candidate in ranked:
                    _check_cancel(cancel)
                    if candidate.kind == "video":
                        continue
                    try:
                        taken_at = _read_capture_datetime(candidate.path)
                    except ImportError:
                        metadata_available = False
                        result.warnings.append(
                            "Capture metadata is unavailable (install exifread); "
                            "using file modification dates."
                        )
                        break
                    except Exception as exc:
                        # Malformed vendor metadata must not prevent the rest of a card
                        # from being previewed. File copying still verifies the snapshot.
                        result.warnings.append(f"Cannot read capture date for {candidate.path.name}: {exc}")
                    if taken_at is not None:
                        date_source = "capture"
                        break
            if taken_at is None:
                # A sidecar's later edit time must never move its photo to a new date.
                taken_at = datetime.fromtimestamp(min(candidate.info.st_mtime for candidate in media))
            if options.cutoff is not None and taken_at.date() < options.cutoff:
                result.filtered += len(candidates)
                continue
            date_folder = Path(f"{taken_at.year:04d}") / f"{taken_at.month:02d}" / f"{taken_at.day:02d}"
            for candidate in candidates:
                result.items.append(MediaItem(
                    source=candidate.path,
                    relative_destination=date_folder / candidate.path.name,
                    size=candidate.info.st_size,
                    taken_at=taken_at,
                    date_source=date_source,
                    kind=candidate.kind,
                    mtime_ns=candidate.info.st_mtime_ns,
                    group_id=str(parent / stem),
                    device=candidate.info.st_dev,
                    inode=candidate.info.st_ino,
                ))
                if options.date_basis == "capture" and date_source == "modified":
                    result.fallback_count += 1
            if progress:
                progress(found, f"Reading dates · {media[0].path.name}")
    except _Cancelled:
        result.cancelled = True
    result.items.sort(key=lambda item: (str(item.relative_destination.parent), str(item.source)))
    return result


def _same_snapshot(item: MediaItem, info: os.stat_result) -> bool:
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_size == item.size
        and (item.mtime_ns is None or info.st_mtime_ns == item.mtime_ns)
        and (item.device is None or info.st_dev == item.device)
        and (item.inode is None or info.st_ino == item.inode)
    )


def _check_source(item: MediaItem) -> os.stat_result:
    info = item.source.stat(follow_symlinks=False)
    if not _same_snapshot(item, info):
        raise OSError("Source changed since the preview; scan again.")
    return info


def _open_source(item: MediaItem) -> int:
    descriptor = os.open(item.source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not _same_snapshot(item, os.fstat(descriptor)):
            raise OSError("Source changed since the preview; scan again.")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _open_directory(path: Path, *, create: bool = True) -> int:
    """Walk with directory handles so a destination symlink cannot redirect writes."""
    if not path.is_absolute():
        raise ValueError("Destination paths must be absolute.")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
            if part in {".", ".."}:
                raise ValueError("Destination paths cannot contain traversal components.")
            try:
                child = os.open(part, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(part, dir_fd=descriptor)
                except FileExistsError:
                    pass
                child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _digest(descriptor: int, cancel: Event | None) -> bytes:
    digest = hashlib.sha256()
    while True:
        _check_cancel(cancel)
        chunk = os.read(descriptor, COPY_CHUNK_SIZE)
        if not chunk:
            return digest.digest()
        digest.update(chunk)


def _source_digest(item: MediaItem, cache: dict, cancel: Event | None) -> bytes:
    _check_source(item)
    key = (item.source, item.size, item.mtime_ns, item.device, item.inode)
    if key not in cache:
        descriptor = _open_source(item)
        try:
            digest = _digest(descriptor, cancel)
            if not _same_snapshot(item, os.fstat(descriptor)):
                raise OSError("Source changed while checking duplicates; scan again.")
            cache[key] = digest
        finally:
            os.close(descriptor)
    return cache[key]


def _existing_matches(
    directory: int, name: str, item: MediaItem, cache: dict, cancel: Event | None,
) -> bool | None:
    """None means absent; false includes symlinks and non-file collisions."""
    try:
        info = os.stat(name, dir_fd=directory, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size != item.size:
        return False
    source_digest = _source_digest(item, cache, cancel)
    descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory)
    try:
        before = os.fstat(descriptor)
        key = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns)
        if key not in cache:
            cache[key] = _digest(descriptor, cancel)
        after = os.fstat(descriptor)
        if key != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise OSError("An existing destination file changed while checking duplicates.")
        return cache[key] == source_digest
    finally:
        os.close(descriptor)


def _renamed_path(relative: Path, number: int) -> Path:
    if number == 1:
        return relative
    stem = _base_stem(relative)
    tail = relative.name[len(stem):]
    return relative.with_name(f"{stem}__{number}{tail}")


def _split_case_collisions(members: list[MediaItem]) -> list[list[MediaItem]]:
    """Separate case-only duplicate names before targeting a case-insensitive disk."""
    lanes: list[list[MediaItem]] = []
    for item in sorted(members, key=lambda member: (_base_stem(member.source), member.source.name)):
        stem = _base_stem(item.source)
        preferred = sorted(lanes, key=lambda lane: not any(_base_stem(member.source) == stem for member in lane))
        for lane in preferred:
            if all(member.relative_destination.name.casefold() != item.relative_destination.name.casefold() for member in lane):
                lane.append(item)
                break
        else:
            lanes.append([item])
    return lanes


def _publish_temp(directory: int, temporary_name: str, final_name: str) -> None:
    """Publish atomically, refusing to replace even a file created a moment ago."""
    if sys.platform == "darwin":
        # Apple renameatx_np(..., RENAME_EXCL) also works on volumes without links.
        function = ctypes.CDLL(None, use_errno=True).renameatx_np
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        if function(directory, os.fsencode(temporary_name), directory, os.fsencode(final_name), 0x4):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), final_name)
    else:
        # A hard link is an atomic, no-replace publication on POSIX filesystems.
        os.link(temporary_name, final_name, src_dir_fd=directory, dst_dir_fd=directory, follow_symlinks=False)
        os.unlink(temporary_name, dir_fd=directory)


def _copy_file(
    item: MediaItem,
    directory: int,
    name: str,
    cancel: Event | None,
    advanced: Callable[[int], None],
) -> None:
    _check_cancel(cancel)
    source = _open_source(item)
    temporary_name = f".camera-import-{uuid.uuid4().hex}.tmp"
    temporary = None
    try:
        source_info = os.fstat(source)
        temporary = os.open(
            temporary_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600, dir_fd=directory,
        )
        copied = 0
        while True:
            _check_cancel(cancel)
            chunk = os.read(source, COPY_CHUNK_SIZE)
            if not chunk:
                break
            remaining = memoryview(chunk)
            while remaining:
                written = os.write(temporary, remaining)
                if not written:
                    raise OSError("Copy stopped before all bytes could be written.")
                remaining = remaining[written:]
            copied += len(chunk)
            advanced(len(chunk))
        _check_cancel(cancel)
        if copied != item.size or not _same_snapshot(item, os.fstat(source)):
            raise OSError("Source changed during the copy; scan again.")
        _check_source(item)
        os.fchmod(temporary, stat.S_IMODE(source_info.st_mode))
        os.utime(temporary, ns=(source_info.st_atime_ns, source_info.st_mtime_ns))
        os.fsync(temporary)
        os.close(temporary)
        temporary = None
        _check_cancel(cancel)
        _publish_temp(directory, temporary_name, name)
        try:
            os.fsync(directory)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise
    finally:
        os.close(source)
        if temporary is not None:
            os.close(temporary)
        try:
            os.unlink(temporary_name, dir_fd=directory)
        except FileNotFoundError:
            pass


def _free_bytes(directory: int) -> int:
    info = os.fstatvfs(directory)
    return info.f_bavail * info.f_frsize


def import_media(
    scan: ScanResult,
    cancel: Event | None = None,
    progress: Callable[[int, int, str], None] | None = None,
) -> ImportResult:
    """Copy a preview safely. Completed files survive cancellation; temps do not."""
    result = ImportResult()
    if scan.cancelled:
        result.cancelled = True
        return result
    options = _validated_options(scan.options)
    # Keep the preview's canonical destination: a newly introduced symlink is an error.
    if options.destination != scan.options.destination:
        raise ValueError("The destination changed since the preview; scan again.")
    if options.source != scan.options.source:
        raise ValueError("The source changed since the preview; scan again.")
    total = scan.total_bytes
    done = 0
    cache: dict = {}
    groups: dict[tuple[str, Path], list[MediaItem]] = defaultdict(list)
    for item in scan.items:
        relative = item.relative_destination
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 4:
            raise ValueError("The preview contains an unsafe destination path.")
        if not item.source.is_relative_to(options.source):
            raise ValueError("The preview contains a file outside the source folder.")
        group_id = item.group_id or str(item.source.parent / _base_stem(item.source).casefold())
        groups[(group_id, relative.parent)].append(item)

    def report(message: str) -> None:
        if progress:
            progress(min(done, total), total, message)

    plans: list[_PlannedItem] = []
    reservations: dict[str, _PlannedItem] = {}
    reserved_stems: set[tuple[Path, str]] = set()
    existing_stems: dict[Path, set[str]] = {}
    required = 0
    try:
        _check_cancel(cancel)
        if not scan.items:
            report("Nothing to import")
            return result
        root = _open_directory(options.destination)
        try:
            import_groups = [
                (date_folder, lane)
                for (_, date_folder), members in groups.items()
                for lane in _split_case_collisions(members)
            ]
            for date_folder, members in import_groups:
                _check_cancel(cancel)
                report(f"Checking duplicates · {members[0].source.name}")
                directory = None
                try:
                    for item in members:
                        _check_source(item)
                    directory = _open_directory(options.destination / date_folder)
                    if date_folder not in existing_stems:
                        with os.scandir(directory) as entries:
                            existing_stems[date_folder] = {
                                _base_stem(Path(entry.name)).casefold()
                                for entry in entries if _kind(Path(entry.name)) is not None
                            }
                    number = 1
                    while True:
                        _check_cancel(cancel)
                        candidates = [
                            _PlannedItem(item, _renamed_path(item.relative_destination, number))
                            for item in members
                        ]
                        statuses: list[bool | None] = []
                        anchors: set[str] = set()
                        conflict = False
                        for index, planned in enumerate(candidates):
                            relative = planned.relative_destination
                            reserved = reservations.get(str(relative).casefold())
                            if reserved is not None:
                                matches = (
                                    reserved.item.size == planned.item.size
                                    and _source_digest(reserved.item, cache, cancel)
                                    == _source_digest(planned.item, cache, cancel)
                                )
                                if matches:
                                    # Also share one actual filename on case-sensitive disks.
                                    candidates[index] = _PlannedItem(planned.item, reserved.relative_destination)
                            else:
                                matches = _existing_matches(directory, relative.name, planned.item, cache, cancel)
                            if matches is False:
                                conflict = True
                                break
                            if matches is True and planned.item.kind != "sidecar":
                                anchors.add(_base_stem(relative).casefold())
                            statuses.append(matches)
                        candidate_stems = {
                            _base_stem(planned.relative_destination).casefold()
                            for planned in candidates
                        }
                        if not conflict:
                            # A disjoint extension isn't proof of the same photograph.
                            # Adding a JPEG beside an unrelated existing RAW would make
                            # Lightroom pair two different shots. Only an identical
                            # shared media file can establish an existing group's identity.
                            conflict = any(
                                stem not in anchors and (
                                    stem in existing_stems[date_folder]
                                    or (date_folder, stem) in reserved_stems
                                )
                                for stem in candidate_stems
                            )
                        if not conflict:
                            for planned, matches in zip(candidates, statuses):
                                key = str(planned.relative_destination).casefold()
                                if matches is None and key not in reservations:
                                    required += planned.item.size
                                reservations[key] = planned
                                plans.append(planned)
                            reserved_stems.update((date_folder, stem) for stem in candidate_stems)
                            break
                        number += 1
                except (OSError, ValueError) as exc:
                    result.errors.extend(f"{item.source.name}: {exc}" for item in members)
                    done += sum(item.size for item in members)
                    report(f"Could not prepare {members[0].source.name}")
                finally:
                    if directory is not None:
                        os.close(directory)
            if required > _free_bytes(root):
                result.errors.append(f"Not enough free space: the import needs {required:,} bytes.")
                return result
        finally:
            os.close(root)

        for planned in plans:
            _check_cancel(cancel)
            item = planned.item
            report(f"Importing {item.source.name}")
            directory = None
            advanced_bytes = 0

            def advanced(count: int) -> None:
                nonlocal done, advanced_bytes
                advanced_bytes += count
                done += count
                report(f"Copying {item.source.name}")

            try:
                _check_source(item)
                directory = _open_directory(options.destination / planned.relative_destination.parent)
                matches = _existing_matches(directory, planned.relative_destination.name, item, cache, cancel)
                if matches is True:
                    result.skipped += 1
                elif matches is False:
                    raise OSError("Destination changed during import; scan and try again.")
                else:
                    _copy_file(item, directory, planned.relative_destination.name, cancel, advanced)
                    result.copied += 1
                    result.bytes_copied += item.size
                    if planned.relative_destination.name != item.relative_destination.name:
                        result.renamed += 1
                result.files.append(ImportedFile(
                    options.destination / planned.relative_destination, item.kind,
                ))
            except (OSError, ValueError) as exc:
                result.errors.append(f"{item.source.name}: {exc}")
            finally:
                if directory is not None:
                    os.close(directory)
            done += item.size - advanced_bytes
            report(f"Processed {item.source.name}")
    except _Cancelled:
        result.cancelled = True
        report("Cancelled")
    except OSError as exc:
        result.errors.append(str(exc))
    return result
