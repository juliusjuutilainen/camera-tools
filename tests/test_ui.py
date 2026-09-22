"""Exercise the actual desktop workflow without a display or user photographs."""

import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from datetime import datetime
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from PySide6.QtCore import QDate, QSettings, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from camera_tools.core import ScanResult, Source
from camera_tools.ui import ImportWindow, STYLE


class DesktopWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle("Fusion")
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "card" / "DCIM"
        self.source.mkdir(parents=True)
        self.destination = self.root / "library"
        self.window = ImportWindow(
            QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat),
            auto_detect=False,
        )
        self.window.source.setEditText(str(self.source))
        self.window.destination.setText(str(self.destination))
        self.window.basis.setCurrentIndex(1)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.wait_for(lambda: self.window.task is None and self.window.detector is None)
        self.app.processEvents()
        self.temp.cleanup()

    def wait_for(self, condition, timeout=5):
        deadline = time.monotonic() + timeout
        while not condition():
            self.app.processEvents()
            if time.monotonic() > deadline:
                self.fail("UI task did not finish")
            time.sleep(0.005)
        self.app.processEvents()

    def photo(self, name, when=datetime(2026, 9, 20, 12)):
        path = self.source / name
        path.write_bytes((name + " photo contents").encode())
        os.utime(path, (when.timestamp(), when.timestamp()))
        return path

    def click_scan(self):
        QTest.mouseClick(self.window.preview_button, Qt.MouseButton.LeftButton)
        self.wait_for(lambda: self.window.task is None)

    def test_preview_copy_pair_repeat_and_invalidate(self):
        for name in ("DSC001.JPG", "DSC001.NEF", "DSC001.xmp"):
            self.photo(name)
        self.click_scan()
        self.assertEqual(self.window.model.rowCount(), 3)
        self.assertFalse(self.destination.exists(), "Preview must be read-only")
        self.assertTrue(self.window.import_button.isEnabled())
        QTest.mouseClick(self.window.import_button, Qt.MouseButton.LeftButton)
        self.wait_for(lambda: self.window.task is None)
        self.assertEqual(self.window.status.text(), "Import complete")
        self.assertIn("3 copied", self.window.activity.text())
        self.assertEqual(len(list((self.destination / "2026/09/20").iterdir())), 3)
        self.assertFalse(self.window.import_button.isEnabled())
        self.click_scan()
        statuses = {self.window.model.data(self.window.model.index(row, 2)) for row in range(3)}
        self.assertEqual(statuses, {"Already present"})
        self.assertEqual(self.window.import_button.text(), "Verify existing files")
        self.assertEqual(self.window.count.text(), "0")
        self.assertIn("3 already present", self.window.preview_note.text())
        self.window.start_import()
        self.wait_for(lambda: self.window.task is None)
        self.assertIn("3 already present", self.window.activity.text())
        (self.destination / "2026/09/20/DSC001.NEF").write_bytes(b"a different photo with the same name")
        self.photo("DSC002.JPG")
        self.click_scan()
        statuses = {self.window.model.data(self.window.model.index(row, 0)): self.window.model.data(self.window.model.index(row, 2)) for row in range(4)}
        self.assertEqual(statuses, {
            "DSC001.JPG": "Already present", "DSC001.NEF": "Name in use",
            "DSC001.xmp": "Already present", "DSC002.JPG": "New",
        })
        self.assertEqual(self.window.import_button.text(), "Import 2 files")
        self.window.videos.setChecked(False)
        self.assertIsNone(self.window.scan)
        self.assertFalse(self.window.import_button.isEnabled())
        self.assertEqual(self.window.model.rowCount(), 0)

    def test_calendar_cutoff_and_invalid_destination(self):
        self.photo("BEFORE.JPG", datetime(2026, 9, 19, 23, 59))
        self.photo("ON.JPG", datetime(2026, 9, 20))
        self.window.cutoff.setDate(QDate(2026, 9, 20))
        self.window.use_cutoff.setChecked(True)
        self.click_scan()
        self.assertEqual([item.source.name for item in self.window.scan.items], ["ON.JPG"])
        self.window.destination.clear()
        self.click_scan()
        self.assertEqual(self.window.status.text(), "Could not finish")
        self.assertIn("destination", self.window.activity.text())
        self.assertFalse(self.destination.exists())

    def test_device_selection_multiple_cards_and_unplug(self):
        self.window.source.setEditText("")
        card = Source("CAMERA", self.source)
        second = Source("SECOND", self.root / "second")
        self.window.devices_found([card, second])
        self.assertEqual(self.window.source.currentText(), "")
        self.window.devices_found([card])
        self.assertEqual(self.window.source.currentText(), str(self.source))
        self.photo("ONE.JPG")
        self.click_scan()
        self.window.devices_found([])
        self.assertEqual(self.window.source.currentText(), "")
        self.assertIsNone(self.window.scan)
        self.window.source.setEditText(str(self.root / "manual"))
        self.window.devices_found([card])
        self.assertEqual(self.window.source.currentText(), str(self.root / "manual"))

    def test_background_scan_remains_responsive_and_closes_safely(self):
        ticks = []
        timer = QTimer()
        timer.setInterval(10)
        timer.timeout.connect(lambda: ticks.append(1))
        timer.start()

        def slow_scan(options, cancel, progress):
            cancel.wait(3)
            return ScanResult(options, [], 0, [], 0, cancelled=cancel.is_set())

        with patch("camera_tools.ui.scan_media", side_effect=slow_scan):
            self.window.start_scan()
            self.wait_for(lambda: len(ticks) >= 5)
            self.assertFalse(self.window.controls.isEnabled())
            self.window.close()
            self.wait_for(lambda: self.window.task is None)
        timer.stop()
        self.assertFalse(self.window.isVisible())
        self.assertFalse(self.destination.exists())


if __name__ == "__main__":
    unittest.main()
