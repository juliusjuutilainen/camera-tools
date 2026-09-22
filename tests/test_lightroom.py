"""The Lightroom helper protocol and catalog-facing paths use disposable files."""

from dataclasses import replace
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from camera_tools import core, lightroom


class LightroomBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root / "Muistikortti '夏' $(untouched)"
        self.destination = self.root / "Photos & Library"
        self.job = self.root / "Lightroom job"
        self.source.mkdir()
        self.job.mkdir()
        self.when = datetime(2026, 9, 19, 13, 30)

    def photo(self, name="IMG.JPG", content=b"photo"):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        timestamp = int(self.when.timestamp()) * 1_000_000_000 + 123_456_789
        os.utime(path, ns=(timestamp, timestamp))
        return path

    def output(self, name="IMG.JPG"):
        return self.destination / "2026/09/19" / name

    def request(self, command, **changes):
        value = {
            "protocol": 1, "command": command, "source": str(self.source),
            "destination": str(self.destination), "date_basis": "modified",
            "include_videos": True, "cutoff": "",
        }
        value.update(changes)
        lightroom._atomic_json(self.job / "request.json", value)

    def run_helper(self, command, **changes):
        self.request(command, **changes)
        completed = subprocess.run(
            [sys.executable, "-m", "camera_tools.lightroom", "--job", str(self.job)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
        )
        response = json.loads((self.job / "response.json").read_text(encoding="utf-8"))
        self.assertEqual(response["protocol"], 1)
        self.assertEqual(completed.returncode, 0 if response["status"] == "ok" else 1, completed.stderr)
        self.assertEqual(completed.stdout, "")
        self.assertEqual(list(self.job.glob("*.tmp")), [])
        return response

    def test_subprocess_preview_copy_and_retry_use_frozen_unicode_paths(self):
        filename = "Kuva '夏' & $(untouched).JPG"
        source = self.photo(filename)
        preview = self.run_helper("preview")
        self.assertEqual(preview["status"], "ok")
        self.assertEqual(preview["count"], 1)
        self.assertEqual(preview["total_bytes"], source.stat().st_size)
        self.assertEqual(preview["source"], str(self.source))
        self.assertEqual(preview["destination"], str(self.destination))
        self.assertIn(filename, preview["preview_text"])
        self.assertIn("PHOTO | 2026-09-19 13:30:00", preview["preview_text"])
        self.assertIn("5 bytes", preview["preview_text"])
        self.assertIn("2026/09/19/", preview["preview_text"])
        self.assertTrue(preview["preview_text"].splitlines()[1].endswith(" | New"))
        self.assertEqual((preview["present"], preview["taken"], preview["bytes_to_copy"]), (0, 0, 5))
        self.assertFalse(self.destination.exists())
        self.photo("LATER.JPG")
        # Copy uses the snapshot, regardless of changes to the request options.
        copied = self.run_helper("copy", source=None, destination="/ignored", date_basis="invalid", cutoff="nonsense")
        self.assertEqual((copied["copied"], copied["skipped"], copied["errors"]), (1, 0, []))
        self.assertEqual(copied["files"], [{"path": str(self.output(filename)), "kind": "photo"}])
        self.assertEqual(self.output(filename).read_bytes(), source.read_bytes())
        self.assertFalse(self.output("LATER.JPG").exists())
        retry = self.run_helper("copy")
        self.assertEqual((retry["copied"], retry["skipped"]), (0, 1))
        self.assertEqual(retry["files"], copied["files"])
        progress = json.loads((self.job / "progress.json").read_text())
        self.assertEqual((progress["phase"], progress["completed"], progress["total"]), ("copy", 5, 5))

    def test_collisions_rename_only_the_colliding_file_and_exclude_sidecars(self):
        self.photo("IMG.JPG", b"new jpeg")
        self.photo("IMG.RAF", b"new raw")
        self.photo("IMG.JPG.xmp", b"edits")
        self.photo("CLIP.MOV", b"video")
        self.output().parent.mkdir(parents=True)
        self.output().write_bytes(b"other jpeg")
        preview = self.run_helper("preview")
        self.assertEqual((preview["present"], preview["taken"]), (0, 1))
        self.assertIn('"IMG.JPG" | PHOTO', preview["preview_text"])
        self.assertIn("| Name in use", preview["preview_text"])
        result = self.run_helper("copy")
        self.assertEqual((result["copied"], result["renamed"], result["errors"]), (4, 1, []))
        self.assertEqual(len(result["notes"]), 1)
        self.assertIn("IMG__2.JPG", result["notes"][0])
        self.assertEqual({(entry["path"], entry["kind"]) for entry in result["files"]}, {
            (str(self.output("IMG__2.JPG")), "photo"),
            (str(self.output("IMG.RAF")), "raw"),
            (str(self.output("CLIP.MOV")), "video"),
        })
        self.assertTrue(self.output("IMG.JPG.xmp").exists())
        repeated = self.run_helper("copy")
        self.assertEqual((repeated["copied"], repeated["skipped"], repeated["errors"]), (0, 4, []))
        self.assertEqual(repeated["files"], result["files"])

    def test_preview_reports_files_already_present_and_copy_skips_them(self):
        self.photo("IMG.JPG", b"photo")
        self.photo("IMG.RAF", b"raw data")
        self.output().parent.mkdir(parents=True)
        self.output().write_bytes(b"photo")
        preview = self.run_helper("preview")
        self.assertEqual((preview["count"], preview["present"], preview["taken"]), (2, 1, 0))
        self.assertEqual((preview["total_bytes"], preview["bytes_to_copy"]), (13, 8))
        self.assertIn('"IMG.JPG" | PHOTO', preview["preview_text"])
        self.assertIn("| Already present", preview["preview_text"])
        snapshot = json.loads((self.job / "preview.json").read_text())
        self.assertEqual({row["source"].rsplit("/", 1)[1]: (row["status"], row["existing_size"]) for row in snapshot["items"]}, {
            "IMG.JPG": ("present", 5), "IMG.RAF": ("new", None),
        })
        copied = self.run_helper("copy")
        self.assertEqual((copied["copied"], copied["skipped"], copied["renamed"], copied["errors"]), (1, 1, 0, []))
        self.assertEqual({entry["path"] for entry in copied["files"]}, {str(self.output()), str(self.output("IMG.RAF"))})

    def test_shared_duplicate_path_is_listed_once(self):
        self.photo("A/IMG.JPG")
        self.photo("B/IMG.JPG")
        self.run_helper("preview")
        copied = self.run_helper("copy")
        self.assertEqual((copied["copied"], copied["skipped"]), (1, 1))
        self.assertEqual(copied["files"], [{"path": str(self.output()), "kind": "photo"}])

    def test_snapshot_integer_precision_round_trips_without_resolving_paths(self):
        self.photo()
        scan = core.scan_media(core.ImportOptions(self.source, self.destination, date_basis="modified"))
        original = scan.items[0]
        scan.items[0] = replace(original, inode=2**63 + 101, mtime_ns=1_789_900_011_123_456_789)
        lightroom._save_preview(self.job / "preview.json", scan)
        restored = lightroom._load_preview(self.job / "preview.json")
        self.assertEqual(restored, scan)
        self.assertIs(type(restored.items[0].mtime_ns), int)
        self.assertIs(type(restored.items[0].inode), int)

    def test_changed_and_missing_sources_are_excluded_but_other_files_continue(self):
        changed = self.photo("CHANGED.JPG")
        missing = self.photo("MISSING.JPG")
        self.photo("OK.JPG")
        self.run_helper("preview")
        changed.write_bytes(b"changed since preview")
        missing.unlink()
        result = self.run_helper("copy")
        self.assertEqual(result["copied"], 1)
        self.assertEqual(len(result["errors"]), 2)
        self.assertIn("Source changed since the preview", "\n".join(result["errors"]))
        self.assertEqual(result["files"], [{"path": str(self.output("OK.JPG")), "kind": "photo"}])

    def test_source_replaced_with_same_size_and_time_is_rejected(self):
        source = self.photo()
        self.run_helper("preview")
        original = source.stat()
        replacement = self.root / "replacement.JPG"
        replacement.write_bytes(b"other")
        os.utime(replacement, ns=(original.st_atime_ns, original.st_mtime_ns))
        os.replace(replacement, source)
        response = self.run_helper("copy")
        self.assertEqual(response["copied"], 0)
        self.assertEqual(response["files"], [])
        self.assertIn("Source changed", response["errors"][0])

    def test_source_root_symlink_change_does_not_retarget_frozen_preview(self):
        self.photo()
        self.run_helper("preview")
        moved = self.root / "moved card"
        self.source.rename(moved)
        self.source.symlink_to(moved, target_is_directory=True)
        response = self.run_helper("copy")
        self.assertEqual(response["status"], "error")
        self.assertIn("source changed", response["error"].lower())
        self.assertFalse(self.destination.exists())

    def test_destination_symlink_changes_are_rejected(self):
        self.photo()
        self.run_helper("preview")
        outside = self.root / "outside"
        outside.mkdir()
        self.destination.symlink_to(outside, target_is_directory=True)
        response = self.run_helper("copy")
        self.assertEqual(response["status"], "error")
        self.assertIn("destination changed", response["error"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_destination_date_symlinks_are_rejected(self):
        self.photo()
        self.run_helper("preview")
        outside = self.root / "outside"
        outside.mkdir()
        date_folder = self.output().parent
        date_folder.parent.mkdir(parents=True)
        date_folder.symlink_to(outside, target_is_directory=True)
        response = self.run_helper("copy")
        self.assertEqual(response["files"], [])
        self.assertTrue(response["errors"])
        self.assertEqual(list(outside.iterdir()), [])

    def test_cancelled_preview_cannot_be_copied(self):
        self.photo()
        (self.job / "cancel").touch()
        preview = self.run_helper("preview")
        self.assertTrue(preview["cancelled"])
        (self.job / "cancel").unlink()
        copied = self.run_helper("copy")
        self.assertEqual(copied["status"], "error")
        self.assertIn("preview was cancelled", copied["error"])
        self.assertFalse(self.destination.exists())

    def test_copy_cancel_file_prevents_copy_and_allows_retry(self):
        self.photo()
        self.run_helper("preview")
        (self.job / "cancel").touch()
        copied = self.run_helper("copy")
        self.assertTrue(copied["cancelled"])
        self.assertEqual(copied["files"], [])
        self.assertEqual(copied["copied"], 0)
        (self.job / "cancel").unlink()
        self.assertEqual(self.run_helper("copy")["copied"], 1)

    def test_cancel_between_files_returns_only_completed_files(self):
        self.photo("FIRST.JPG")
        self.photo("SECOND.JPG")
        self.run_helper("preview")
        self.request("copy")

        def cancel_after_first(scan, cancel, progress):
            def report(done, total, message):
                progress(done, total, message)
                if message == "Processed FIRST.JPG":
                    (self.job / "cancel").touch()
                    cancel.next_check = 0
            return core.import_media(scan, cancel, report)

        with patch.object(lightroom, "import_media", side_effect=cancel_after_first):
            response = lightroom.run_job(self.job)
        self.assertTrue(response["cancelled"])
        self.assertEqual(response["copied"], 1)
        self.assertEqual(response["files"], [{"path": str(self.output("FIRST.JPG")), "kind": "photo"}])
        self.assertFalse(self.output("SECOND.JPG").exists())

    def test_cancel_mid_file_removes_temporary_and_omits_pending_path(self):
        self.photo(content=b"a" * 64)
        self.run_helper("preview")
        self.request("copy")

        def cancel_during_copy(scan, cancel, progress):
            def report(done, total, message):
                progress(done, total, message)
                if done:
                    (self.job / "cancel").touch()
                    cancel.next_check = 0
            return core.import_media(scan, cancel, report)

        with patch.object(lightroom, "import_media", side_effect=cancel_during_copy), patch.object(core, "COPY_CHUNK_SIZE", 8):
            response = lightroom.run_job(self.job)
        self.assertTrue(response["cancelled"])
        self.assertEqual(response["files"], [])
        self.assertEqual(response["copied"], 0)
        self.assertEqual(list(self.destination.rglob("*.tmp")), [])
        self.assertFalse(self.output().exists())

    def test_invalid_request_fields_have_error_response(self):
        self.photo()
        for changes in [
            {"protocol": True}, {"protocol": 99}, {"command": "erase"},
            {"source": ""}, {"destination": None}, {"source": "bad\0path"},
            {"cutoff": "2026-02-30"}, {"cutoff": "20260919"}, {"cutoff": 123},
            {"include_videos": "false"}, {"date_basis": "utc"},
        ]:
            with self.subTest(changes=changes):
                response = self.run_helper("preview", **changes) if "command" not in changes else self.run_helper(changes["command"])
                self.assertEqual(response["status"], "error")
                self.assertTrue(response["error"])
        self.assertFalse(self.destination.exists())

    def test_bad_json_and_duplicate_fields_are_rejected(self):
        for contents in ["[1,2]", "{broken}", '{"protocol":1,"protocol":1,"command":"discover"}', '{"protocol":1,"value":NaN}']:
            with self.subTest(contents=contents):
                (self.job / "request.json").write_text(contents, encoding="utf-8")
                self.assertEqual(lightroom.main(["--job", str(self.job)]), 1)
                response = json.loads((self.job / "response.json").read_text())
                self.assertEqual(response["status"], "error")

    def test_invalid_replacement_preview_removes_old_snapshot(self):
        self.photo()
        self.run_helper("preview")
        self.assertEqual(self.run_helper("preview", cutoff="invalid")["status"], "error")
        self.assertFalse((self.job / "preview.json").exists())
        copied = self.run_helper("copy")
        self.assertEqual(copied["status"], "error")
        self.assertIn("Preview", copied["error"])

    def test_malformed_snapshot_cannot_escape_destination_or_weaken_identity_check(self):
        self.photo()
        self.run_helper("preview")
        original = json.loads((self.job / "preview.json").read_text())
        mutations = [
            ("relative_destination", "../../outside.JPG"),
            ("source", str(self.root / "outside.JPG")),
            ("mtime_ns", None), ("inode", 123.5), ("device", True),
            ("size", -1), ("kind", "executable"), ("status", "maybe"), ("existing_size", "5"),
        ]
        for key, value in mutations:
            with self.subTest(field=key):
                snapshot = json.loads(json.dumps(original))
                snapshot["items"][0][key] = value
                lightroom._atomic_json(self.job / "preview.json", snapshot)
                response = self.run_helper("copy")
                self.assertEqual(response["status"], "error")
        self.assertFalse(self.destination.exists())

    def test_preview_filters_are_applied_to_snapshot(self):
        self.photo("STILL.JPG")
        self.photo("CLIP.MOV")
        response = self.run_helper("preview", cutoff="2026-09-19", include_videos=False)
        self.assertEqual((response["count"], response["filtered"]), (1, 1))
        self.assertEqual(self.run_helper("copy")["copied"], 1)
        response = self.run_helper("preview", cutoff="2026-09-20")
        self.assertEqual((response["count"], response["filtered"]), (0, 2))

    def test_preview_text_contains_every_item(self):
        for index in range(125):
            self.photo(f"FRAME_{index:04}.JPG")
        response = self.run_helper("preview")
        self.assertEqual(len(response["preview_text"].splitlines()), 126)
        self.assertIn("FRAME_0124.JPG", response["preview_text"])

    def test_discovery_protocol_and_no_qt_import(self):
        with patch.object(lightroom, "discover_sources", return_value=[core.Source("My camera", self.source)]):
            self.request("discover")
            response = lightroom.run_job(self.job)
        self.assertEqual(response["sources"], [{"label": "My camera", "path": str(self.source)}])
        self.assertEqual(self.run_helper("discover")["status"], "ok")
        completed = subprocess.run(
            [sys.executable, "-c", "import camera_tools.lightroom, sys; assert not any(name.startswith('PySide6') for name in sys.modules)"],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_atomic_response_write_preserves_previous_file_on_failure(self):
        response = self.job / "response.json"
        lightroom._atomic_json(response, {"protocol": 1, "status": "old"})
        with patch.object(lightroom.os, "replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                lightroom._atomic_json(response, {"protocol": 1, "status": "new"})
        self.assertEqual(json.loads(response.read_text())["status"], "old")
        self.assertEqual(list(self.job.glob("*.tmp")), [])

    def test_progress_and_cancel_files_are_throttled(self):
        progress = lightroom._Progress(self.job / "progress.json", "scan")
        with patch.object(lightroom.time, "monotonic", side_effect=[10.0, 10.1, 10.3]):
            progress.report(1, 10, "first")
            progress.report(2, 10, "second")
            self.assertEqual(json.loads(progress.path.read_text())["completed"], 1)
            progress.report(3, 10, "third")
        self.assertEqual(json.loads(progress.path.read_text())["completed"], 3)
        cancel = lightroom._FileCancellation(self.job / "cancel")
        with patch.object(lightroom.time, "monotonic", side_effect=[10.0, 10.01, 10.1]):
            self.assertFalse(cancel.is_set())
            cancel.path.touch()
            self.assertFalse(cancel.is_set())
            self.assertTrue(cancel.is_set())
        cancel.path.unlink()
        self.assertTrue(cancel.is_set())


if __name__ == "__main__":
    unittest.main()
