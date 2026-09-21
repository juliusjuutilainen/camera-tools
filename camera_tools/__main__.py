"""Desktop application, with a preview-first command-line alternative."""

import argparse
from datetime import date
from pathlib import Path
import sys
import threading


def main(argv=None):
    parser = argparse.ArgumentParser(description="Import photos into YYYY/MM/DD, keeping RAW + JPEG together.")
    parser.add_argument("--list-devices", action="store_true", help="List mounted camera cards and exit")
    parser.add_argument("--source", type=Path, help="Source folder (opens the command-line preview)")
    parser.add_argument("--destination", type=Path, default=Path.home() / "Pictures")
    parser.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD", help="Include this date and later")
    parser.add_argument("--date-basis", choices=("capture", "modified"), default="capture")
    parser.add_argument("--photos-only", action="store_true")
    parser.add_argument("--copy", action="store_true", help="Copy after scanning; otherwise only preview")
    args = parser.parse_args(argv)
    from .core import ImportOptions, discover_sources, import_media, scan_media

    if args.list_devices:
        devices = discover_sources()
        for device in devices:
            print(f"{device.label}\t{device.path}")
        if not devices:
            print("No mounted camera or SD card detected. You can also choose a source folder.")
        return 0
    if args.source is None:
        if args.copy or args.since or args.photos_only or args.date_basis != "capture":
            parser.error("command-line import options require --source")
        try:
            from .ui import run
        except ModuleNotFoundError as error:
            if error.name and error.name.startswith("PySide6"):
                print("Install dependencies and launch with: uv run camera-import", file=sys.stderr)
                return 1
            raise
        return run()
    try:
        options = ImportOptions(
            source=args.source, destination=args.destination,
            cutoff=args.since, include_videos=not args.photos_only, date_basis=args.date_basis,
        )
        scan = scan_media(options)
        print(f"{len(scan.items):,} files · {scan.total_bytes / (1024 ** 2):,.1f} MiB · {scan.filtered:,} excluded by filters")
        for item in scan.items[:30]:
            print(f"  {item.source.name} → {item.relative_destination} [{item.date_source}]")
        if len(scan.items) > 30:
            print(f"  … and {len(scan.items) - 30:,} more")
        for warning in scan.warnings:
            print(f"Warning: {warning}", file=sys.stderr)
        if not args.copy:
            print("Preview only. Add --copy to import these files.")
            return 0
        cancel = threading.Event()
        import signal
        previous = signal.signal(signal.SIGINT, lambda *_: cancel.set())
        try:
            result = import_media(scan, cancel=cancel)
        finally:
            signal.signal(signal.SIGINT, previous)
        print(f"Copied {result.copied:,}; already present {result.skipped:,}; renamed {result.renamed:,}.")
        for error in result.errors:
            print(error, file=sys.stderr)
        return 130 if result.cancelled else (1 if result.errors else 0)
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
