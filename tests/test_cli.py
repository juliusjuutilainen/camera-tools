from contextlib import redirect_stdout, redirect_stderr
from datetime import datetime
import io
import os
from pathlib import Path
import tempfile
import unittest

from camera_tools.__main__ import main


class CommandLineTests(unittest.TestCase):
    def test_preview_then_explicit_copy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "card"
            source.mkdir()
            photo = source / "PHOTO.JPG"
            photo.write_bytes(b"test photograph")
            timestamp = datetime(2026, 9, 20).timestamp()
            os.utime(photo, (timestamp, timestamp))
            destination = root / "library"
            arguments = ["--source", str(source), "--destination", str(destination),
                         "--date-basis", "modified", "--since", "2026-09-20"]
            with redirect_stdout(io.StringIO()) as output:
                self.assertEqual(main(arguments), 0)
            self.assertIn("Preview only", output.getvalue())
            self.assertFalse(destination.exists())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(main(arguments + ["--copy"]), 0)
            self.assertEqual((destination / "2026/09/20/PHOTO.JPG").read_bytes(), photo.read_bytes())

    def test_missing_source_error_is_actionable(self):
        with redirect_stderr(io.StringIO()) as error:
            self.assertEqual(main(["--source", "/nonexistent-camera-tools-source"]), 1)
        self.assertIn("source", error.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
