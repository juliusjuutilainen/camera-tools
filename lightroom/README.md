# Camera Tools for Lightroom Classic

The plug-in copies photos from a mounted camera/card or a local folder into your
chosen destination, then adds supported photos to the active Lightroom Classic
catalog. Originals remain on the card. Lightroom Classic on macOS is required;
the cloud-based Lightroom app does not support this plug-in.

## Install

1. Extract `CameraTools-macos-arm64.zip` on an Apple silicon Mac, or use the
   `x86_64` archive built on an Intel Mac. Keep the entire `CameraTools.lrplugin`
   folder together in a permanent location, such as `~/Library/Application
   Support/Adobe/Lightroom/Modules/`.
2. In Lightroom Classic, open **File → Plug-in Manager → Add** and select
   `CameraTools.lrplugin`.
3. Open **File → Plug-in Extras → Import from camera**. Choose the source and
   destination, preview the import, then import the files into your catalog.

The release plug-in includes its Python helper and ExifRead. Installing Python,
uv, or the desktop app is unnecessary. `BUILD.txt` records the package's CPU
architecture and build environment. Builds are ad-hoc signed for local use and
are not notarized for public distribution. Lightroom UI integration requires a
manual check in Lightroom Classic; it cannot be exercised by the Python tests.

## Import and recovery

The destination, date cutoff, date source, and video option are remembered. Cards
are detected when opening the plug-in; **Refresh cards** rescans after connecting
a card. You can also choose or type any source and destination folder. The date
cutoff is inclusive. Capture dates fall back to modification dates when absent;
the preview shows the fallback count and any scan warnings.

**Import files** copies the exact preview snapshot. Changed or disconnected
sources are reported. The existing engine preserves RAW/JPEG/sidecar filenames,
uses matching suffixes for conflicts, and verifies existing matching files by
content. The preview displays the proposed paths; collision suffixes are chosen
during copying. Sidecars are copied beside their media and are not added as
separate catalog entries. The plug-in does not change Lightroom's RAW/JPEG
preferences or create catalog stacks. Lightroom decides which file formats it
can add; a rejected file remains on disk and is listed in the report.

Cancelling a copy retains completed files and adds those completed media files to
the original catalog. Catalog import has its own cancel button. **File → Plug-in
Extras → Retry last camera catalog import…** retries that stage without reading
the card or copying again. It checks for photos already in the catalog and
requires the original catalog to be open. Files that failed to copy require a new
normal import from the card. If Lightroom or the helper exits before producing a
copy report, start a new normal import; completed copies will be verified/reused.

Import reports and the frozen preview are saved under
`CameraTools/Imports` inside Lightroom's SDK `appData` directory. Use **Open import
report** in the result dialog to locate a session. Reports contain local filenames
and paths. Preview-only sessions are removed when closed; copy sessions are kept
for recovery. Nothing is uploaded, and the card is never erased.

## Check in Lightroom Classic

Automated tests exercise the actual Python helper and Lua 5.1 code against a
simulated SDK, including preview cancellation, catalog duplicates, errors, and
retry. They do not verify Lightroom's native UI or its actual RAW/JPEG behavior.
Before using this build for a full card, use a temporary catalog and a small
source folder containing a real JPEG, RAW, and matching XMP:

1. Confirm both folder pickers, the inclusive cutoff, preview pages, and saved
   destination work. Cancelling the preview must copy nothing.
2. Import and confirm the date folders, original bytes, sidecars, and catalog
   entries. Repeat the import and verify that no additional copies or catalog
   entries appear.
3. Try different contents with the same filenames and check the matching suffixes.
4. Cancel copying and catalog import separately, then use the retry command.
   Confirm it refuses to run against a different catalog.
5. Check a mounted card, unsupported file, and disconnected source. These hardware
   and host behaviors have not been tested on the development machine.

SDK references: [Adobe Lightroom Classic SDK](https://developer.adobe.com/lightroom-classic)
and [Adobe SDK programmer's guide](https://ioconsolerykerprodcdn.azureedge.net/static/installers/lr/sdk/2022/cross_platform/v13/doc/Lightroom%20Classic%20SDK%20Guide_1655133965.pdf).
`Json.lua` is the MIT-licensed [rxi/json.lua](https://github.com/rxi/json.lua),
version 0.1.2; its license is included in the file.

## Build from source

From the repository on macOS:

```sh
uv run --with pyinstaller python scripts/build_lightroom_plugin.py
```

The builder produces:

- `dist/CameraTools.lrplugin` — the installable plug-in folder.
- `dist/CameraTools-macos-arm64.zip` (or `x86_64`) — the same folder for transport.

The helper is built for the architecture of the Python interpreter running the
builder. Build separately on each target architecture. Compatibility with macOS
versions older than the build host has not been verified. The helper uses
PyInstaller's one-directory format and excludes Qt; all files under `bin/` must
stay together. The ZIP preserves executable permissions and internal symlinks.
Extract with Finder or `ditto -x -k`; Python's `zipfile.extractall()` does not
preserve these properties.

Only named artifacts in `build/` and `dist/` are replaced. The source plug-in is
not modified. Use the locked repository dependencies and the same Python and
PyInstaller versions recorded in `BUILD.txt` when repeating a release build.
ZIP entries use stable ordering and timestamps; `SOURCE_DATE_EPOCH` can override
the default timestamp. Bit-for-bit identical signed binaries across build hosts
are not guaranteed.

Build references: [PyInstaller usage](https://pyinstaller.org/en/stable/usage.html),
[macOS builds](https://pyinstaller.org/en/stable/feature-notes.html#macos-multi-arch-support),
and [symlink preservation](https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#requirements-imposed-by-symbolic-links-in-frozen-application).
