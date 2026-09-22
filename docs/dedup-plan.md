# Plan: per-file deduplication within the destination date folder

Goal: a file whose name already exists in its target `yyyy/mm/dd` folder is never
imported again, and the preview shows this before anything is copied.

## Gap today

- `scan_media` never looks at the destination, so the preview lists every file as if
  it will be copied. The skip only happens inside `import_media`.
- Duplicate handling is group-wide: JPEG, RAW and sidecars share one verdict. When one
  member differs (or a sidecar changed) the whole group is copied again with `__2`,
  duplicating the identical members.

## Rule

Each file is judged on its own; JPEG and RAW are independent. Only the planned
destination folder is consulted (no library-wide search).

| Destination `yyyy/mm/dd/<name>` | Preview status | Import action |
|---|---|---|
| absent | New | copy |
| exists, same size | Already present | verify content (SHA-256, as today); identical → skip; different → copy as `<stem>__2<ext>` and report it |
| exists, different size | Exists, different size | copy as `<stem>__2<ext>` |
| exists but is not a regular file (symlink, dir) | Conflict | error for that file, others continue |

Sidecars follow the same per-file rule (an unchanged `.xmp` is skipped, a changed
one gets `__2`). Case-insensitive name matching, as the destination disk is.

Consequence to accept: with no group coupling, a new `DSC01234.JPG` can land beside an
unrelated existing `DSC01234.ARW` and Lightroom will pair them. This only happens after
a camera filename reset within one day. The preview shows both files' statuses.

## Changes

### `core.py`
- `MediaItem` gains `status: str` (`new | present | size_differs | conflict`) and
  `existing_size: int | None`.
- `scan_media`: after grouping, one `os.scandir` per distinct date folder that exists
  (stat-only, cached per folder, cancellable, progress "Checking existing files").
  No file content is read during preview.
- `ScanResult` gains `present`, `renamed_preview`, `bytes_to_copy` so the UI can show
  "N new · N already present" and size the progress bar on files actually copied.
- `import_media`: replace the group reservation loop (`_split_case_collisions`,
  anchors, `existing_stems`, `reserved_stems`) with a per-file plan: present → content
  check → skip or `__N`; size_differs → `__N`; new → copy. Keep every safety guard
  (snapshot checks, temp file + no-replace publish, free-space check, cancellation).
  `_renamed_path` stays per file. Files planned in this run still reserve their names
  so two source folders with the same filename get distinct suffixes.
- `ImportResult.skipped` keeps meaning "identical, not copied"; `renamed` counts
  suffixed copies. `notes` lists each suffixed file with the reason.

### Surfaces
- `ui.py`: `PreviewModel` gets a **Status** column; muted row colour for "Already
  present"; totals line "12 new · 30 already present · 1 exists with different size";
  import button reads "Import 12 files"; progress total = `bytes_to_copy`.
- `__main__.py`: preview lines show `[new]` / `[present]`; summary unchanged plus notes.
- `lightroom.py`: `preview_text` gains a Status column; `preview.json` stores `status`
  and `existing_size`; response gains `present` count. `PROTOCOL` stays 1 (additive).
  `lightroom/CameraTools.lrplugin/App.lua` preview summary shows the present count;
  rebuild `dist/`.
- README: rewrite the duplicates section (per-file, same folder, preview shows it).

### Tests (unittest, temp dirs)
- preview marks present / size_differs / new correctly; no content read during scan
  (extend `test_modified_mode_never_reads_metadata_or_hashes_during_scan`).
- JPEG present + RAW new → only RAW copied, JPEG skipped, no suffix
  (replaces `test_partial_existing_pair_does_not_split_when_raw_collides`).
- same name + size, different content → `__2` for that file only.
- changed sidecar, identical photo → photo skipped, sidecar `__2`.
- same filename from two source folders → second gets `__2`; rerun is idempotent.
- destination file replaced between preview and import → re-verified at copy time.
- remove/adjust group-coupling tests: `test_unrelated_existing_raw_reserves_basename_
  for_new_jpeg`, `test_shared_identical_media_anchor_can_complete_existing_pair`,
  `test_matching_sidecar_alone_does_not_establish_media_identity`,
  `test_conflicting_pair_gets_common_suffix_and_rerun_is_idempotent`.
- ui: Status column and totals; lightroom: preview_text/response fields; cli: labels.

## Delivery
Standard tier, one repo, one `software-engineer` (opus, medium): core first, then
surfaces, README, plug-in rebuild. `verifier` reruns
`uv run python -m unittest discover -s tests -v` and the `--with lupa` variant.
