"""Behavior and file-safety tests; all imports use disposable fixtures."""

import os
import tempfile
import threading
import unittest
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from camera_tools import core
from camera_tools.core import ImportOptions, discover_sources, import_media, scan_media


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "CARD"
        self.destination = self.root / "Photos"
        self.source.mkdir()
        self.when = datetime(2026, 9, 19, 13, 30)

    def photo(self, relative="DCIM/100CAM/IMG_0001.JPG", content=b"jpeg", when=None):
        path = self.source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        stamp = (when or self.when).timestamp()
        os.utime(path, (stamp, stamp))
        return path

    def options(self, **kwargs):
        return ImportOptions(self.source, self.destination, date_basis="modified", **kwargs)

    def output(self, name, when=None):
        when = when or self.when
        return self.destination / f"{when.year:04d}/{when.month:02d}/{when.day:02d}" / name

    def test_pair_and_sidecars_share_yyyy_mm_dd_and_preserve_mtime(self):
        jpeg = self.photo()
        raw = self.photo("DCIM/100CAM/IMG_0001.RAF", b"raw", datetime(2026, 9, 20, 1))
        self.photo("DCIM/100CAM/IMG_0001.xmp", b"edits", datetime(2026, 9, 21))
        self.photo("DCIM/100CAM/IMG_0001.JPG.aae", b"apple", datetime(2026, 9, 22))
        preview = scan_media(self.options())
        self.assertEqual({item.relative_destination.parent for item in preview.items}, {Path("2026/09/19")})
        result = import_media(preview)
        self.assertEqual((result.copied, result.skipped, result.errors), (4, 0, []))
        self.assertEqual(self.output(jpeg.name).read_bytes(), b"jpeg")
        self.assertEqual(self.output(raw.name).stat().st_mtime_ns, raw.stat().st_mtime_ns)
        self.assertEqual(jpeg.read_bytes(), b"jpeg")
        self.assertEqual(raw.read_bytes(), b"raw")

    def test_cutoff_is_inclusive_and_filters_pair_together(self):
        self.photo("before.JPG", when=datetime(2026, 9, 18, 23, 59, 59))
        self.photo("boundary.JPG", when=datetime(2026, 9, 19))
        self.photo("boundary.RAF", when=datetime(2026, 9, 20))
        self.photo("boundary.xmp", when=datetime(2026, 9, 21))
        self.photo("later.JPG", when=datetime(2026, 9, 20))
        preview = scan_media(self.options(cutoff=date(2026, 9, 19)))
        self.assertEqual(preview.filtered, 1)
        self.assertEqual({item.source.name for item in preview.items}, {
            "boundary.JPG", "boundary.RAF", "boundary.xmp", "later.JPG",
        })
        preview = scan_media(self.options(cutoff=date(2026, 9, 20)))
        self.assertEqual([item.source.name for item in preview.items], ["later.JPG"])
        self.assertEqual(preview.filtered, 4)

    def test_capture_prefers_jpeg_date_for_raw_pair_and_sidecar(self):
        jpeg = self.photo()
        self.photo("DCIM/100CAM/IMG_0001.RAF", b"raw")
        self.photo("DCIM/100CAM/IMG_0001.xmp", b"metadata")
        capture = datetime(2023, 1, 2, 0, 0)
        with patch.object(core, "_read_capture_datetime", return_value=capture) as read:
            preview = scan_media(replace(self.options(), date_basis="capture"))
        read.assert_called_once_with(jpeg)
        self.assertEqual(preview.fallback_count, 0)
        self.assertEqual({item.relative_destination.parent for item in preview.items}, {Path("2023/01/02")})
        self.assertEqual({item.date_source for item in preview.items}, {"capture"})

    def test_raw_capture_date_is_used_when_jpeg_has_none(self):
        self.photo()
        self.photo("DCIM/100CAM/IMG_0001.RAF", b"raw")
        with patch.object(core, "_read_capture_datetime", side_effect=[None, datetime(2022, 5, 1)]) as read:
            preview = scan_media(replace(self.options(), date_basis="capture"))
        self.assertEqual(read.call_count, 2)
        self.assertTrue(all(item.taken_at == datetime(2022, 5, 1) for item in preview.items))

    def test_missing_metadata_reader_falls_back_once_with_warning(self):
        self.photo("one.JPG")
        self.photo("two.RAF")
        with patch.object(core, "_read_capture_datetime", side_effect=ImportError):
            preview = scan_media(replace(self.options(), date_basis="capture"))
        self.assertEqual(preview.fallback_count, 2)
        self.assertEqual(len(preview.warnings), 1)
        self.assertTrue(all(item.taken_at == self.when for item in preview.items))

    def test_exif_reader_disables_thumbnails_and_stops_at_original_date(self):
        path = self.photo()
        with patch("exifread.process_file", return_value={"EXIF DateTimeOriginal": "2021:12:31 23:59:59"}) as read:
            self.assertEqual(core._read_capture_datetime(path), datetime(2021, 12, 31, 23, 59, 59))
        self.assertEqual(read.call_args.kwargs, {
            "details": False, "extract_thumbnail": False, "stop_tag": "DateTimeOriginal",
        })

    def test_modified_mode_never_reads_metadata_or_hashes_during_scan(self):
        self.photo()
        with patch.object(core, "_read_capture_datetime", side_effect=AssertionError), patch.object(core, "_digest", side_effect=AssertionError):
            preview = scan_media(self.options())
        self.assertEqual(len(preview.items), 1)

    def test_video_toggle_and_orphan_sidecars(self):
        self.photo("still.JPG")
        self.photo("movie.MOV", b"movie")
        self.photo("movie.xmp", b"metadata")
        self.photo("orphan.xmp", b"metadata")
        self.photo("notes.txt", b"notes")
        preview = scan_media(self.options(include_videos=False))
        self.assertEqual([item.source.name for item in preview.items], ["still.JPG"])
        self.assertEqual(preview.filtered, 3)
        preview = scan_media(self.options())
        self.assertEqual({item.source.name for item in preview.items}, {"still.JPG", "movie.MOV", "movie.xmp"})

    def test_hidden_files_and_source_symlinks_are_not_followed(self):
        self.photo("visible.JPG")
        self.photo(".hidden/hidden.JPG")
        self.photo(".hidden.JPG")
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "outside.JPG").write_bytes(b"outside")
        (self.source / "linked-directory").symlink_to(outside, target_is_directory=True)
        (self.source / "linked.JPG").symlink_to(outside / "outside.JPG")
        preview = scan_media(self.options())
        self.assertEqual([item.source.name for item in preview.items], ["visible.JPG"])

    def test_scan_cancellation_is_reported(self):
        self.photo("one.JPG")
        self.photo("two.JPG")
        cancel = threading.Event()
        preview = scan_media(self.options(), cancel, lambda *_: cancel.set())
        self.assertTrue(preview.cancelled)
        self.assertTrue(import_media(preview).cancelled)
        self.assertFalse(self.destination.exists())

    def test_repeat_import_skips_only_content_verified_duplicates(self):
        self.photo()
        self.photo("DCIM/100CAM/IMG_0001.RAF", b"raw")
        preview = scan_media(self.options())
        first = import_media(preview)
        self.assertEqual(first.copied, 2)
        with patch.object(core, "_free_bytes", return_value=0):
            again = import_media(preview)
        self.assertEqual((again.copied, again.skipped, again.errors), (0, 2, []))

    def test_no_hashing_when_destination_names_are_new(self):
        self.photo()
        preview = scan_media(self.options())
        with patch.object(core, "_digest", side_effect=AssertionError("Unneeded hashing")):
            result = import_media(preview)
        self.assertEqual((result.copied, result.errors), (1, []))

    def test_conflicting_pair_gets_common_suffix_and_rerun_is_idempotent(self):
        self.photo()
        self.photo("DCIM/100CAM/IMG_0001.RAF", b"raw")
        self.photo("DCIM/100CAM/IMG_0001.JPG.xmp", b"metadata")
        existing = self.output("IMG_0001.JPG")
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"old!")  # Same size, different contents.
        preview = scan_media(self.options())
        result = import_media(preview)
        self.assertEqual((result.copied, result.renamed, result.errors), (3, 3, []))
        self.assertEqual(existing.read_bytes(), b"old!")
        self.assertEqual(self.output("IMG_0001__2.JPG").read_bytes(), b"jpeg")
        self.assertEqual(self.output("IMG_0001__2.RAF").read_bytes(), b"raw")
        self.assertEqual(self.output("IMG_0001__2.JPG.xmp").read_bytes(), b"metadata")
        again = import_media(preview)
        self.assertEqual((again.copied, again.skipped, again.errors), (0, 3, []))
        self.assertFalse(self.output("IMG_0001__3.JPG").exists())

    def test_partial_existing_pair_does_not_split_when_raw_collides(self):
        self.photo()
        self.photo("DCIM/100CAM/IMG_0001.RAF", b"raw")
        self.output("IMG_0001.JPG").parent.mkdir(parents=True)
        self.output("IMG_0001.JPG").write_bytes(b"jpeg")
        self.output("IMG_0001.RAF").write_bytes(b"old")
        result = import_media(scan_media(self.options()))
        self.assertEqual((result.copied, result.renamed, result.skipped), (2, 2, 0))
        self.assertEqual(self.output("IMG_0001__2.JPG").read_bytes(), b"jpeg")
        self.assertEqual(self.output("IMG_0001__2.RAF").read_bytes(), b"raw")

    def test_same_names_in_two_camera_folders_keep_both_pairs(self):
        for folder, data in [("100CAM", b"first"), ("101CAM", b"other")]:
            self.photo(f"DCIM/{folder}/IMG_0001.JPG", data)
            self.photo(f"DCIM/{folder}/IMG_0001.RAF", data + b"raw")
        preview = scan_media(self.options())
        result = import_media(preview)
        self.assertEqual((result.copied, result.renamed, result.errors), (4, 2, []))
        self.assertEqual(self.output("IMG_0001.JPG").read_bytes(), b"first")
        self.assertEqual(self.output("IMG_0001__2.JPG").read_bytes(), b"other")
        again = import_media(preview)
        self.assertEqual((again.copied, again.skipped, again.errors), (0, 4, []))

    def test_disjoint_formats_from_different_folders_do_not_form_false_pair(self):
        self.photo("100CAM/IMG_0001.JPG", b"shot one")
        self.photo("101CAM/IMG_0001.NEF", b"different shot")
        preview = scan_media(self.options())
        result = import_media(preview)
        self.assertEqual((result.copied, result.renamed, result.errors), (2, 1, []))
        self.assertEqual(self.output("IMG_0001.JPG").read_bytes(), b"shot one")
        self.assertFalse(self.output("IMG_0001.NEF").exists())
        self.assertEqual(self.output("IMG_0001__2.NEF").read_bytes(), b"different shot")
        again = import_media(preview)
        self.assertEqual((again.copied, again.skipped, again.errors), (0, 2, []))

    def test_unrelated_existing_raw_reserves_basename_for_new_jpeg(self):
        self.photo()
        raw = self.output("IMG_0001.NEF")
        raw.parent.mkdir(parents=True)
        raw.write_bytes(b"unrelated existing raw")
        preview = scan_media(self.options())
        result = import_media(preview)
        self.assertEqual((result.copied, result.renamed, result.errors), (1, 1, []))
        self.assertFalse(self.output("IMG_0001.JPG").exists())
        self.assertEqual(self.output("IMG_0001__2.JPG").read_bytes(), b"jpeg")
        self.assertEqual(raw.read_bytes(), b"unrelated existing raw")
        self.assertEqual(import_media(preview).skipped, 1)

    def test_shared_identical_media_anchor_can_complete_existing_pair(self):
        self.photo()
        self.photo("DCIM/100CAM/IMG_0001.RAF", b"raw")
        jpeg = self.output("IMG_0001.JPG")
        jpeg.parent.mkdir(parents=True)
        jpeg.write_bytes(b"jpeg")
        result = import_media(scan_media(self.options()))
        self.assertEqual((result.copied, result.skipped, result.renamed, result.errors), (1, 1, 0, []))
        self.assertEqual(self.output("IMG_0001.RAF").read_bytes(), b"raw")

    def test_matching_sidecar_alone_does_not_establish_media_identity(self):
        self.photo()
        self.photo("DCIM/100CAM/IMG_0001.xmp", b"common edit settings")
        existing = self.output("IMG_0001.NEF")
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"unrelated raw")
        self.output("IMG_0001.xmp").write_bytes(b"common edit settings")
        result = import_media(scan_media(self.options()))
        self.assertEqual((result.copied, result.renamed, result.errors), (2, 2, []))
        self.assertTrue(self.output("IMG_0001__2.JPG").exists())
        self.assertTrue(self.output("IMG_0001__2.xmp").exists())

    def test_identical_files_from_multiple_folders_share_verified_copy(self):
        self.photo("A/IMG.JPG", b"same")
        self.photo("B/IMG.JPG", b"same")
        result = import_media(scan_media(self.options()))
        self.assertEqual((result.copied, result.skipped, result.errors), (1, 1, []))

    def test_case_only_destination_collisions_keep_both_pairs(self):
        # Simulate a case-sensitive card even when the test volume is APFS default.
        for name, content in [("UPPER.JPG", b"upper"), ("UPPER.RAF", b"upper raw"),
                              ("lower.jpg", b"lower"), ("lower.raf", b"lower raw")]:
            self.photo(name, content)
        preview = scan_media(self.options())
        names = {"UPPER.JPG": "IMG.JPG", "UPPER.RAF": "IMG.RAF", "lower.jpg": "img.jpg", "lower.raf": "img.raf"}
        preview.items = [replace(
            item, group_id="one-case-insensitive-group",
            relative_destination=item.relative_destination.with_name(names[item.source.name]),
        ) for item in preview.items]
        result = import_media(preview)
        self.assertEqual((result.copied, result.renamed, result.errors), (4, 2, []))
        self.assertEqual(self.output("IMG.JPG").read_bytes(), b"upper")
        self.assertEqual(self.output("IMG.RAF").read_bytes(), b"upper raw")
        self.assertEqual(self.output("img__2.jpg").read_bytes(), b"lower")
        self.assertEqual(self.output("img__2.raf").read_bytes(), b"lower raw")
        again = import_media(preview)
        self.assertEqual((again.copied, again.skipped, again.errors), (0, 4, []))

    def test_insufficient_space_prevents_copying(self):
        self.photo()
        with patch.object(core, "_free_bytes", return_value=0):
            result = import_media(scan_media(self.options()))
        self.assertEqual(result.copied, 0)
        self.assertIn("Not enough free space", result.errors[0])
        self.assertFalse(self.output("IMG_0001.JPG").exists())

    def test_source_changed_since_preview_errors_and_other_files_continue(self):
        changed = self.photo("changed.JPG")
        self.photo("unchanged.JPG")
        preview = scan_media(self.options())
        changed.write_bytes(b"new content")
        result = import_media(preview)
        self.assertEqual(result.copied, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("changed since the preview", result.errors[0])
        self.assertFalse(self.output("changed.JPG").exists())
        self.assertTrue(self.output("unchanged.JPG").exists())

    def test_source_disconnected_after_preview_produces_actionable_error(self):
        source = self.photo()
        preview = scan_media(self.options())
        source.unlink()
        result = import_media(preview)
        self.assertEqual(result.copied, 0)
        self.assertEqual(len(result.errors), 1)
        self.assertIn(source.name, result.errors[0])

    def test_per_file_permission_error_does_not_stop_other_imports(self):
        self.photo("locked.JPG")
        self.photo("readable.JPG")
        open_source = core._open_source

        def restricted(item):
            if item.source.name == "locked.JPG":
                raise PermissionError("Permission denied")
            return open_source(item)

        with patch.object(core, "_open_source", side_effect=restricted):
            result = import_media(scan_media(self.options()))
        self.assertEqual(result.copied, 1)
        self.assertEqual(len(result.errors), 1)
        self.assertIn("Permission denied", result.errors[0])
        self.assertTrue(self.output("readable.JPG").exists())

    def test_cancel_mid_file_removes_temporary_copy(self):
        source = self.photo(content=b"x" * 64)
        cancel = threading.Event()

        def progress(done, total, message):
            if done > 0:
                cancel.set()

        with patch.object(core, "COPY_CHUNK_SIZE", 8):
            result = import_media(scan_media(self.options()), cancel, progress)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.copied, 0)
        self.assertEqual(result.bytes_copied, 0)
        self.assertFalse(self.output(source.name).exists())
        self.assertEqual(list(self.destination.rglob("*.tmp")), [])
        self.assertEqual(source.read_bytes(), b"x" * 64)

    def test_cancel_between_files_keeps_completed_copies(self):
        self.photo("first.JPG", b"first")
        self.photo("second.JPG", b"second")
        cancel = threading.Event()

        def progress(done, total, message):
            if message == "Processed first.JPG":
                cancel.set()

        result = import_media(scan_media(self.options()), cancel, progress)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.copied, 1)
        self.assertEqual(self.output("first.JPG").read_bytes(), b"first")
        self.assertFalse(self.output("second.JPG").exists())

    def test_source_changed_during_copy_is_not_published(self):
        source = self.photo(content=b"a" * 32)
        changed = False

        def progress(done, total, message):
            nonlocal changed
            if done and not changed:
                changed = True
                source.write_bytes(b"b" * 33)

        with patch.object(core, "COPY_CHUNK_SIZE", 8):
            result = import_media(scan_media(self.options()), progress=progress)
        self.assertEqual(result.copied, 0)
        self.assertIn("Source changed during the copy", result.errors[0])
        self.assertFalse(self.output(source.name).exists())
        self.assertEqual(list(self.destination.rglob("*.tmp")), [])

    def test_destination_date_symlink_cannot_escape_library(self):
        self.photo()
        outside = self.root / "outside"
        outside.mkdir()
        date_folder = self.output("IMG_0001.JPG").parent
        date_folder.parent.mkdir(parents=True)
        date_folder.symlink_to(outside, target_is_directory=True)
        result = import_media(scan_media(self.options()))
        self.assertEqual(result.copied, 0)
        self.assertTrue(result.errors)
        self.assertEqual(list(outside.iterdir()), [])

    def test_destination_replaced_by_symlink_after_preview_is_rejected(self):
        self.photo()
        preview = scan_media(self.options())
        outside = self.root / "outside"
        outside.mkdir()
        self.destination.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(ValueError, "destination changed"):
            import_media(preview)
        self.assertEqual(list(outside.iterdir()), [])

    def test_existing_destination_file_symlink_is_preserved_not_followed(self):
        self.photo()
        outside = self.root / "outside.JPG"
        outside.write_bytes(b"do not change")
        self.output("IMG_0001.JPG").parent.mkdir(parents=True)
        self.output("IMG_0001.JPG").symlink_to(outside)
        result = import_media(scan_media(self.options()))
        self.assertEqual((result.copied, result.renamed, result.errors), (1, 1, []))
        self.assertEqual(outside.read_bytes(), b"do not change")
        self.assertEqual(self.output("IMG_0001__2.JPG").read_bytes(), b"jpeg")

    def test_late_destination_collision_is_never_overwritten(self):
        self.photo()
        publish = core._publish_temp

        def collide(directory, temporary_name, final_name):
            descriptor = os.open(final_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=directory)
            try:
                os.write(descriptor, b"other import")
            finally:
                os.close(descriptor)
            publish(directory, temporary_name, final_name)

        with patch.object(core, "_publish_temp", side_effect=collide):
            result = import_media(scan_media(self.options()))
        self.assertEqual(result.copied, 0)
        self.assertTrue(result.errors)
        self.assertEqual(self.output("IMG_0001.JPG").read_bytes(), b"other import")
        self.assertEqual(list(self.destination.rglob("*.tmp")), [])

    def test_progress_finishes_at_total_including_skipped_files(self):
        self.photo("one.JPG", b"1")
        self.photo("two.JPG", b"22")
        preview = scan_media(self.options())
        updates = []
        result = import_media(preview, progress=lambda *args: updates.append(args))
        self.assertEqual(result.bytes_copied, 3)
        self.assertEqual(updates[-1][:2], (3, 3))
        self.assertEqual([update[0] for update in updates], sorted(update[0] for update in updates))
        updates.clear()
        import_media(preview, progress=lambda *args: updates.append(args))
        self.assertEqual(updates[-1][:2], (3, 3))

    def test_overlap_and_unsafe_preview_are_rejected(self):
        for destination in [self.source, self.source / "nested", self.root]:
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                scan_media(replace(self.options(), destination=destination))
        self.photo()
        preview = scan_media(self.options())
        preview.items[0] = replace(preview.items[0], relative_destination=Path("../../outside.JPG"))
        with self.assertRaises(ValueError):
            import_media(preview)

    def test_discovery_requires_camera_signature_and_ignores_symlinks(self):
        (self.source / "DCIM").mkdir()
        ordinary = self.root / "ordinary"
        ordinary.mkdir()
        second = self.root / "second"
        (second / "PRIVATE").mkdir(parents=True)
        link = self.root / "linked-card"
        link.symlink_to(self.source, target_is_directory=True)
        with patch.object(core, "_volume_candidates", return_value=[ordinary, self.source, second, link, Path("/")]):
            sources = discover_sources()
        self.assertEqual({source.path for source in sources}, {self.source, second})


if __name__ == "__main__":
    unittest.main()
