"""Scan mounted cards and copy media into YYYY/MM/DD without overwriting files.

Scanning never reads entire files or writes to the library. Capture dates are camera
wall-clock dates, so timezone conversion cannot move a photograph to another day.
RAW/JPEG pairs and their sidecars share a date; duplicates and name collisions are
judged file by file against the planned date folder, and the preview reports them.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import shutil
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
_WINDOWS = sys.platform == "win32"
# A present file with the same size and a modification time this close is trusted
# without reading it (the copy preserved timestamps; FAT cards keep 2-second stamps).
MTIME_TOLERANCE_NS = 2 * 1_000_000_000
# Windows os.open defaults to text mode, which would rewrite bytes inside photos.
_O_BINARY = getattr(os, "O_BINARY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
# Preview status of a file against its planned date folder (stat only, no hashing).
STATUS_NEW = "new"            # No file with this name in the date folder.
STATUS_PRESENT = "present"    # Same name and size: verified by content and skipped.
STATUS_TAKEN = "taken"        # Same name, different file: copied with a suffix.
STATUS_LABELS = {STATUS_NEW: "New", STATUS_PRESENT: "Already present", STATUS_TAKEN: "Name in use"}


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
    status: str = STATUS_NEW
    existing_size: int | None = None


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

    @property
    def present_count(self) -> int:
        return sum(1 for item in self.items if item.status == STATUS_PRESENT)

    @property
    def taken_count(self) -> int:
        return sum(1 for item in self.items if item.status == STATUS_TAKEN)

    @property
    def import_count(self) -> int:
        """Files the import will copy: everything not already present."""
        return len(self.items) - self.present_count

    @property
    def bytes_to_copy(self) -> int:
        return sum(item.size for item in self.items if item.status != STATUS_PRESENT)


@dataclass
class ImportResult:
    copied: int = 0
    skipped: int = 0
    renamed: int = 0
    errors: list[str] = field(default_factory=list)
    cancelled: bool = False
    bytes_copied: int = 0
    files: list[ImportedFile] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


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
                    if _WINDOWS:
                        # DirEntry.stat() leaves st_dev and st_ino at zero on Windows.
                        info = os.lstat(path)
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
        result.items = _mark_existing(result.items, options.destination, result.warnings, cancel, progress, found)
    except _Cancelled:
        result.cancelled = True
    result.items.sort(key=lambda item: (str(item.relative_destination.parent), str(item.source)))
    return result


def _existing_entries(directory: _Directory | Path) -> dict[str, dict[str, int | None]]:
    """Case-folded name -> {actual name: size of a regular file, None otherwise}."""
    entries: dict[str, dict[str, int | None]] = defaultdict(dict)
    listing = directory.scandir() if isinstance(directory, _Directory) else os.scandir(directory)
    with listing:
        for entry in listing:
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            entries[entry.name.casefold()][entry.name] = info.st_size if stat.S_ISREG(info.st_mode) else None
    return entries


def _lookup(entries: dict[str, dict[str, int | None]], name: str) -> tuple[str, int | None] | None:
    """Find a name as a case-insensitive destination disk would, preferring an exact match."""
    variants = entries.get(name.casefold())
    if not variants:
        return None
    actual = name if name in variants else min(variants)
    return actual, variants[actual]


def _mark_existing(
    items: list[MediaItem],
    destination: Path,
    warnings: list[str],
    cancel: Event | None,
    progress: Callable[[int, str], None] | None,
    found: int,
) -> list[MediaItem]:
    """Compare each file with its planned date folder by name and size; read nothing.

    This is a read-only look, so plain paths are enough and it also works where the
    import's directory handles are unavailable (Windows). The import re-checks safely.
    """
    if not destination.is_dir():
        return items
    listings: dict[Path, dict[str, dict[str, int | None]] | None] = {}
    marked: list[MediaItem] = []
    for item in items:
        folder = item.relative_destination.parent
        if folder not in listings:
            _check_cancel(cancel)
            if progress:
                progress(found, f"Checking existing files · {folder}")
            path = destination / folder
            try:
                if path.is_symlink():
                    raise OSError("the date folder is a symbolic link")
                listings[folder] = _existing_entries(path) if path.is_dir() else {}
            except OSError as exc:
                warnings.append(f"Cannot check existing files in {folder}: {exc}")
                listings[folder] = None
        entries = listings[folder]
        existing = _lookup(entries, item.relative_destination.name) if entries is not None else None
        if existing is None:
            marked.append(item)
            continue
        _, size = existing
        status = STATUS_PRESENT if size == item.size else STATUS_TAKEN
        marked.append(replace(item, status=status, existing_size=size))
    return marked


def _same_snapshot(item: MediaItem, info: os.stat_result, *, handle: bool = False) -> bool:
    # Windows handle stats do not reliably carry the ids that lstat reported at scan
    # time, so handle-based checks there rely on type, size and modification time.
    identity = not (handle and _WINDOWS)
    return (
        stat.S_ISREG(info.st_mode)
        and info.st_size == item.size
        and (item.mtime_ns is None or info.st_mtime_ns == item.mtime_ns)
        and (not identity or item.device is None or info.st_dev == item.device)
        and (not identity or item.inode is None or info.st_ino == item.inode)
    )


def _check_source(item: MediaItem) -> os.stat_result:
    info = item.source.stat(follow_symlinks=False)
    if not _same_snapshot(item, info):
        raise OSError("Source changed since the preview; scan again.")
    return info


def _open_source(item: MediaItem) -> int:
    descriptor = os.open(item.source, os.O_RDONLY | _O_NOFOLLOW | _O_BINARY)
    try:
        if not _same_snapshot(item, os.fstat(descriptor), handle=True):
            raise OSError("Source changed since the preview; scan again.")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


class _Directory:
    """One destination folder.

    POSIX keeps a directory descriptor so a symlink swapped in after the check cannot
    redirect writes. Windows has no dir_fd, so it works on the validated path instead
    and every opened file is still verified by handle before it is trusted.
    """

    def __init__(self, path: Path, descriptor: int | None):
        self.path = path
        self.descriptor = descriptor

    def stat(self, name: str) -> os.stat_result:
        if self.descriptor is None:
            return os.lstat(self.path / name)
        return os.stat(name, dir_fd=self.descriptor, follow_symlinks=False)

    def open(self, name: str, flags: int, mode: int = 0o777) -> int:
        flags |= _O_NOFOLLOW | _O_BINARY
        if self.descriptor is None:
            return os.open(self.path / name, flags, mode)
        return os.open(name, flags, mode, dir_fd=self.descriptor)

    def scandir(self):
        return os.scandir(self.path if self.descriptor is None else self.descriptor)

    def unlink(self, name: str) -> None:
        if self.descriptor is None:
            os.unlink(self.path / name)
        else:
            os.unlink(name, dir_fd=self.descriptor)

    def sync(self) -> None:
        if self.descriptor is None:
            return  # Windows cannot flush a directory; the file itself was flushed.
        try:
            os.fsync(self.descriptor)
        except OSError as exc:
            if exc.errno not in {errno.EINVAL, errno.ENOTSUP}:
                raise

    def free_bytes(self) -> int:
        if self.descriptor is None:
            return shutil.disk_usage(self.path).free
        info = os.fstatvfs(self.descriptor)
        return info.f_bavail * info.f_frsize

    def close(self) -> None:
        if self.descriptor is not None:
            os.close(self.descriptor)


def _is_reparse_point(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)
    return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _open_directory(path: Path, *, create: bool = True) -> _Directory:
    """Walk to a destination folder so a symlink cannot redirect writes.

    POSIX walks with directory handles and O_NOFOLLOW. Windows checks every component
    with lstat and refuses symbolic links and junctions.
    """
    if not path.is_absolute():
        raise ValueError("Destination paths must be absolute.")
    if any(part in {".", ".."} for part in path.parts[1:]):
        raise ValueError("Destination paths cannot contain traversal components.")
    if _WINDOWS:
        current = Path(path.anchor)
        for part in path.parts[1:]:
            current = current / part
            try:
                info = os.lstat(current)
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(current)
                except FileExistsError:
                    pass
                info = os.lstat(current)
            if not stat.S_ISDIR(info.st_mode) or _is_reparse_point(info):
                raise OSError(f"{current} is a link or not a folder.")
        return _Directory(path, None)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | _O_NOFOLLOW
    descriptor = os.open(path.anchor, flags)
    try:
        for part in path.parts[1:]:
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
        return _Directory(path, descriptor)
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
            if not _same_snapshot(item, os.fstat(descriptor), handle=True):
                raise OSError("Source changed while checking duplicates; scan again.")
            cache[key] = digest
        finally:
            os.close(descriptor)
    return cache[key]


def _existing_matches(
    directory: _Directory, name: str, item: MediaItem, cache: dict, cancel: Event | None,
) -> bool | None:
    """None means absent; false includes symlinks and non-file collisions.

    Same size and modification time is the quick check that lets a re-import of a
    full card finish in seconds; only a size match with another time is hashed.
    """
    try:
        info = directory.stat(name)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size != item.size:
        return False
    if item.mtime_ns is not None and abs(info.st_mtime_ns - item.mtime_ns) <= MTIME_TOLERANCE_NS:
        return True
    source_digest = _source_digest(item, cache, cancel)
    descriptor = directory.open(name, os.O_RDONLY)
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


_UNSUPPORTED_ERRNOS = {errno.ENOTSUP, errno.EOPNOTSUPP, errno.EPERM, errno.EINVAL}


def _exclusive_rename(directory: _Directory, temporary_name: str, final_name: str) -> None:
    """Atomic no-replace publication where the filesystem offers one (APFS, HFS+, ext4)."""
    if sys.platform == "darwin":
        function = ctypes.CDLL(None, use_errno=True).renameatx_np
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        if function(directory.descriptor, os.fsencode(temporary_name), directory.descriptor, os.fsencode(final_name), 0x4):
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error), final_name)
    else:
        # A hard link is an atomic, no-replace publication on POSIX filesystems.
        os.link(
            temporary_name, final_name,
            src_dir_fd=directory.descriptor, dst_dir_fd=directory.descriptor, follow_symlinks=False,
        )
        os.unlink(temporary_name, dir_fd=directory.descriptor)


def _checked_rename(directory: _Directory, temporary_name: str, final_name: str) -> None:
    """exFAT and FAT have neither exclusive rename nor hard links: check, then rename.

    The window between the check and the rename is microseconds, and the caller
    verified the name a moment earlier, so this is as close to no-replace as those
    filesystems allow.
    """
    try:
        directory.stat(final_name)
    except FileNotFoundError:
        os.rename(
            temporary_name, final_name,
            src_dir_fd=directory.descriptor, dst_dir_fd=directory.descriptor,
        )
    else:
        raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), final_name)


def _step(step: str, exc: OSError) -> OSError:
    """Keep errno and class (FileExistsError stays FileExistsError), name the step."""
    if exc.errno is None:
        return OSError(f"{exc} while {step}")
    return type(exc)(exc.errno, f"{exc.strerror} while {step}", exc.filename)


def _publish_temp(directory: _Directory, temporary_name: str, final_name: str) -> None:
    """Publish without replacing an existing file, even one created a moment ago."""
    if directory.descriptor is None:
        # Windows MoveFileEx without REPLACE_EXISTING: os.rename never overwrites.
        os.rename(directory.path / temporary_name, directory.path / final_name)
        return
    try:
        _exclusive_rename(directory, temporary_name, final_name)
    except OSError as exc:
        if exc.errno not in _UNSUPPORTED_ERRNOS:
            raise
        _checked_rename(directory, temporary_name, final_name)


def _copy_file(
    item: MediaItem,
    directory: _Directory,
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
        temporary = directory.open(temporary_name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
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
        if copied != item.size or not _same_snapshot(item, os.fstat(source), handle=True):
            raise OSError("Source changed during the copy; scan again.")
        _check_source(item)
        times = (source_info.st_atime_ns, source_info.st_mtime_ns)
        if directory.descriptor is not None:
            try:
                os.fchmod(temporary, stat.S_IMODE(source_info.st_mode))
            except OSError as exc:
                # FAT-family volumes have no permission bits worth failing over.
                if exc.errno not in _UNSUPPORTED_ERRNOS:
                    raise _step("setting permissions", exc)
            try:
                os.utime(temporary, ns=times)
            except OSError as exc:
                raise _step("setting timestamps", exc)
        os.fsync(temporary)
        os.close(temporary)
        temporary = None
        if directory.descriptor is None:
            # Windows refuses path-based utime while another handle is open.
            try:
                os.utime(directory.path / temporary_name, ns=times)
            except OSError as exc:
                raise _step("setting timestamps", exc)
        _check_cancel(cancel)
        try:
            _publish_temp(directory, temporary_name, name)
        except OSError as exc:
            raise _step("publishing the copy", exc)
        directory.sync()
    finally:
        os.close(source)
        if temporary is not None:
            os.close(temporary)
        try:
            directory.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _free_bytes(directory: _Directory) -> int:
    return directory.free_bytes()


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
    for item in scan.items:
        relative = item.relative_destination
        if relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 4:
            raise ValueError("The preview contains an unsafe destination path.")
        if not item.source.is_relative_to(options.source):
            raise ValueError("The preview contains a file outside the source folder.")

    def report(message: str) -> None:
        if progress:
            progress(min(done, total), total, message)

    plans: list[_PlannedItem] = []
    reservations: dict[str, _PlannedItem] = {}
    listings: dict[Path, dict[str, dict[str, int | None]]] = {}
    required = 0
    try:
        _check_cancel(cancel)
        if not scan.items:
            report("Nothing to import")
            return result
        root = _open_directory(options.destination)
        try:
            # Every file is judged on its own against its date folder: an identical
            # file is reused, a different file with the same name yields a suffix.
            # JPEG, RAW and sidecars never drag each other into a rename.
            for item in scan.items:
                _check_cancel(cancel)
                report(f"Checking duplicates · {item.source.name}")
                directory = None
                try:
                    _check_source(item)
                    date_folder = item.relative_destination.parent
                    directory = _open_directory(options.destination / date_folder)
                    if date_folder not in listings:
                        listings[date_folder] = _existing_entries(directory)
                    entries = listings[date_folder]
                    number = 1
                    while True:
                        _check_cancel(cancel)
                        relative = _renamed_path(item.relative_destination, number)
                        key = str(relative).casefold()
                        reserved = reservations.get(key)
                        if reserved is not None:
                            if (
                                reserved.item.size == item.size
                                and _source_digest(reserved.item, cache, cancel)
                                == _source_digest(item, cache, cancel)
                            ):
                                # Identical sources share the copy planned earlier in this run.
                                plans.append(_PlannedItem(item, reserved.relative_destination))
                                break
                            number += 1
                            continue
                        existing = _lookup(entries, relative.name)
                        matches = None
                        if existing is not None:
                            matches = _existing_matches(directory, existing[0], item, cache, cancel)
                        if matches is None:
                            planned = _PlannedItem(item, relative)
                            reservations[key] = planned
                            plans.append(planned)
                            required += item.size
                            break
                        if matches:
                            # Report the name the disk actually has, as Lightroom sees it.
                            plans.append(_PlannedItem(item, relative.with_name(existing[0])))
                            break
                        number += 1
                except (OSError, ValueError) as exc:
                    result.errors.append(f"{item.source.name}: {exc}")
                    done += item.size
                    report(f"Could not prepare {item.source.name}")
                finally:
                    if directory is not None:
                        directory.close()
            if required > _free_bytes(root):
                result.errors.append(f"Not enough free space: the import needs {required:,} bytes.")
                return result
        finally:
            root.close()

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
                        result.notes.append(
                            f"{item.source.name} copied as {planned.relative_destination.name}: "
                            "a different file already has its name."
                        )
                result.files.append(ImportedFile(
                    options.destination / planned.relative_destination, item.kind,
                ))
            except (OSError, ValueError) as exc:
                result.errors.append(f"{item.source.name}: {exc}")
            finally:
                if directory is not None:
                    directory.close()
            done += item.size - advanced_bytes
            report(f"Processed {item.source.name}")
    except _Cancelled:
        result.cancelled = True
        report("Cancelled")
    except OSError as exc:
        result.errors.append(str(exc))
    return result
