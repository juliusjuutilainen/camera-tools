"""Release packaging checks that do not require PyInstaller or Lightroom."""

import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile

from scripts.build_lightroom_plugin import create_zip, output_directory


class LightroomPackageTests(unittest.TestCase):
    def test_zip_keeps_executable_and_symlink(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "CameraTools.lrplugin"
            (package / "bin").mkdir(parents=True)
            helper = package / "bin" / "helper"
            helper.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            helper.chmod(0o755)
            (package / "helper-link").symlink_to("bin/helper")
            first, second = root / "first.zip", root / "second.zip"
            create_zip(package, first)
            os.utime(helper, (1700000000, 1700000000))
            create_zip(package, second)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                executable = archive.getinfo("CameraTools.lrplugin/bin/helper")
                self.assertTrue(executable.external_attr >> 16 & stat.S_IXUSR)
                link = archive.getinfo("CameraTools.lrplugin/helper-link")
                self.assertTrue(stat.S_ISLNK(link.external_attr >> 16))
                self.assertEqual(archive.read(link), b"bin/helper")
            if sys.platform == "darwin":
                extracted = root / "extracted"
                subprocess.run(["/usr/bin/ditto", "-x", "-k", str(first), str(extracted)], check=True)
                installed = extracted / package.name
                self.assertTrue((installed / "helper-link").is_symlink())
                subprocess.run([str(installed / "bin" / "helper")], check=True)

    def test_zip_rejects_link_outside_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "CameraTools.lrplugin"
            package.mkdir()
            (package / "outside").symlink_to(root / "other")
            with self.assertRaisesRegex(ValueError, "escapes"):
                create_zip(package, root / "plugin.zip")

    def test_output_rejects_symlinked_build_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "somewhere").mkdir()
            (root / "build").symlink_to(root / "somewhere", target_is_directory=True)
            with self.assertRaisesRegex(ValueError, "symbolic link"):
                output_directory(root, "build/lightroom-plugin")

    def test_output_rejects_path_outside_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "escapes"):
                output_directory(root, "../outside")


if __name__ == "__main__":
    unittest.main()
