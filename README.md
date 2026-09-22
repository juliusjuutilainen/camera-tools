# Photo Importer

A local desktop importer for a Lightroom photo library. Copies photographs into
`yyyy/mm/dd` and keeps same-name JPEG, RAW, and sidecar files together. Originals
stay on the card.

macOS is the primary platform. Windows is supported for preview and import: the
engine walks destination folders by path there, refuses symbolic links and
junctions, and publishes each copy with a no-overwrite rename. The Lightroom
plug-in build is macOS only. Linux mount discovery exists, but Linux hardware has
not been tested.

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

The desktop interface uses Qt instead of Tkinter; a separate Tk installation is
no longer needed. No web service, account, or upload is involved.

## Lightroom Classic plug-in

You can also run the importer from **File → Plug-in Extras → Import from camera…**
in Lightroom Classic on macOS. Choose a source, destination, and optional date
cutoff, review the preview, then copy and automatically add the completed media
to the current catalog. The plug-in uses the same copy engine as the desktop app.

Build the self-contained plug-in for your Mac:

```sh
uv run --with pyinstaller python scripts/build_lightroom_plugin.py
```

In Lightroom Classic, use **File → Plug-in Manager → Add** and select
`dist/CameraTools.lrplugin`. The built plug-in includes Python and ExifRead; it
does not need a separate Python or uv installation. See the
[plug-in guide](lightroom/README.md) for installation, recovery, and validation.

The helper and Lua integration are tested automatically with a simulated SDK.
The native Lightroom dialogs and real catalog behavior still need an in-app
check; Lightroom Classic is not installed on the development machine.

## Import

1. Connect your SD card or camera in **USB mass-storage mode**. Mounted volumes
   with camera folders are detected automatically and refreshed while the app is
   open. A single card is selected automatically; choose the source if several
   are connected. **Choose folder** also accepts any local source directory.
2. Choose the root of your existing photo library, such as `~/Pictures`.
3. Optionally enable **Only on or after** and select the earliest date to include.
   The selected day is included, starting at midnight.
4. Click **Preview import**. Check filenames, dates, sizes, destination paths, and
   the **Status** column: *New*, *Already present* (a file with the same name and
   size is in that date folder), or *Name in use* (a different file has the name).
5. Click **Import files**. Only new files are copied; progress and cancellation
   remain available throughout.
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
  detection compares each file with its own target date folder, not with the
  whole library.
- The date source controls both the cutoff and destination folder. Paired photos
  from the same source directory share one date, preferring the JPEG's capture
  metadata. Sidecars inherit their photo's date. With no capture metadata, the
  group uses a consistent modification date.
- Camera EXIF dates are treated as the camera's local wall-clock date, without
  shifting time zones. File modification dates use the computer's local timezone.
- JPEG, RAW, and matching XMP/AAE files stay together. Different source directories
  are separate groups, so filename resets on a camera do not merge unrelated shots.

### Duplicates, safety, and speed

- Every file is judged on its own against its target date folder. The preview
  lists each planned date folder (names and sizes only, nothing is read or hashed)
  and marks a file **Already present** when a file with the same name and size is
  there, or **Name in use** when a different file has that name. Matching is
  case-insensitive, like the destination disk.
- During import, an *Already present* file is compared by content (SHA-256) with
  the existing copy. Identical files are never copied again. A different file
  with the same name, and every *Name in use* file, is copied with a suffix such
  as `DSC01234__2.JPG`; the import report lists these renames.
- JPEG, RAW, and sidecars are not coupled for duplicate handling: if the JPEG is
  already present and the RAW is new, only the RAW is copied, without a suffix.
  After a camera filename reset a new JPEG can therefore land beside an unrelated
  RAW of the same name in the same day; the preview shows both statuses.
- Scanning reads directory entries and date metadata without hashing every photo
  or extracting thumbnails. The table handles large previews without creating a
  widget for every row.
- Scanning and copying run off the UI thread. Physical transfer speed still
  depends on the card, reader, and destination. Re-importing verifies present
  files by content, so it can take longer than a name-only skip.
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

On Windows the same command takes drive paths, for example `--source E:\ --destination D:\Pictures`.

The preview prints each file's status (`New`, `Already present`, `Name in use`)
and how many bytes the import would copy. Add `--copy` to perform the import.
Other options: `--photos-only` and `--date-basis modified`. Ctrl+C requests safe
cancellation during copying.

## Development

```sh
uv sync
uv run python -m unittest discover -s tests -v
uv run --with lupa python -m unittest discover -s tests -v
```

The core tests use temporary directories, including cancellation, date boundaries,
repeat imports, and filename conflicts. UI tests run with Qt's offscreen platform.
The second command also runs the Lightroom Lua 5.1 integration tests using Lupa;
without Lupa those tests are skipped. It does not require Lightroom or modify a
real catalog.

Implementation references: [Qt for Python](https://doc.qt.io/qtforpython-6/) and
[ExifRead](https://pypi.org/project/ExifRead/).
