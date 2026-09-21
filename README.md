# Photo Importer

A local desktop importer for a Lightroom photo library. Copies photographs into
`yyyy/mm/dd` and keeps same-name JPEG, RAW, and sidecar files together. Originals
stay on the card.

macOS is the supported platform. The filesystem engine also has Linux mount
discovery, but Linux hardware has not been tested; Windows copying is not supported.

## Run

From this repository:

```sh
uv run camera-import
```

`uv` installs the locked dependencies on the first run. Alternatively, use Python
3.11 or newer:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m camera_tools
```

The original command also works once dependencies are installed:

```sh
uv run python scripts/import.py
```

The interface uses Qt instead of Tkinter; a separate Tk installation is no longer
needed. No web service, account, upload, or Lightroom plugin is involved.

## Import

1. Connect your SD card or camera in **USB mass-storage mode**. Mounted volumes
   with camera folders are detected automatically and refreshed while the app is
   open. A single card is selected automatically; choose the source if several
   are connected. **Choose folder** also accepts any local source directory.
2. Choose the root of your existing photo library, such as `~/Pictures`.
3. Optionally enable **Only on or after** and select the earliest date to include.
   The selected day is included, starting at midnight.
4. Click **Preview import**. Check filenames, dates, sizes, and destination paths.
5. Click **Import files**. Progress and cancellation remain available throughout.
6. In Lightroom Classic, use **Import → Add** on the destination folder to add the
   copied files to your catalog. The importer does not edit the Lightroom catalog.

The destination and filter options are remembered between sessions. Source
selection is detected afresh each time.

### Folder layout and dates

```text
Pictures/
  2026/
    09/
      20/
        DSC01234.JPG
        DSC01234.ARW
        DSC01234.xmp
```

- **Date taken** uses embedded EXIF dates when available. Modification time is the
  fallback; the preview explicitly reports how many files use it. Video and
  unsupported metadata formats generally use modification time.
- **File modified date** retains the old importer's date source. Choose it if you
  need to match an existing library organized with the old script. Switching date
  sources can put the same photograph in a different date folder; duplicate
  detection is within each target date folder, not across your whole library.
- The date source controls both the cutoff and destination folder. Paired photos
  from the same source directory share one date, preferring the JPEG's capture
  metadata. Sidecars inherit their photo's date. With no capture metadata, the
  group uses a consistent modification date.
- Camera EXIF dates are treated as the camera's local wall-clock date, without
  shifting time zones. File modification dates use the computer's local timezone.
- JPEG, RAW, and matching XMP/AAE files stay together. Different source directories
  are separate groups, so filename resets on a camera do not merge unrelated shots.

### Duplicates, safety, and speed

- Scanning reads directory entries and date metadata without hashing every photo
  or extracting thumbnails. The table handles large previews without creating a
  widget for every row.
- Scanning and copying run off the UI thread. Physical transfer speed still
  depends on the card, reader, and destination. Re-importing verifies existing
  matching files by content, so it can take longer than the old name-only skip.
- Existing files with matching names and sizes are compared by content during
  import. Identical files are skipped. Different files receive a suffix such as
  `DSC01234__2.JPG`, with the same suffix used for the RAW and sidecars.
- Copies are written to temporary files in the destination, then published
  without overwriting existing photos. File timestamps are preserved.
- Cancellation removes the active temporary copy. Completed files remain;
  preview and import again to resume. The card is never erased or modified.
- Source changes after preview, disconnected cards, insufficient free space, and
  individual copy errors are reported. Review any errors before erasing a card.
- Hidden folders and symbolic links are not traversed. Source and destination
  cannot overlap.

### Device support

Automatic discovery is aimed at SD cards and cameras that appear as mounted
drives and contain standard camera directories (`DCIM` or `PRIVATE`). Manual
folder selection works for other layouts. **PTP/MTP-only USB cameras do not mount
as drives and are not supported by this version.** For those, use a card reader,
switch the camera to mass-storage mode if available, or first download with the
camera's software. No hardware was connected during development; real-device
detection and transfer should be checked with your camera/card.

## Command line

List detected cards:

```sh
uv run camera-import --list-devices
```

Preview without copying:

```sh
uv run camera-import --source /Volumes/CAMERA --destination ~/Pictures --since 2026-09-01
```

Add `--copy` to perform the import. Other options: `--photos-only` and
`--date-basis modified`. Ctrl+C requests safe cancellation during copying.

## Development

```sh
uv sync
uv run python -m unittest discover -s tests -v
```

The core tests use temporary directories, including cancellation, date boundaries,
repeat imports, and filename conflicts. UI tests run with Qt's offscreen platform.

Implementation references: [Qt for Python](https://doc.qt.io/qtforpython-6/) and
[ExifRead](https://pypi.org/project/ExifRead/).
