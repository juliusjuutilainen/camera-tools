"""Real EXIF parsing against a minimal JPEG APP1/TIFF fixture."""

from datetime import date, datetime
import os
from pathlib import Path
import struct
import tempfile
import unittest

from camera_tools.core import ImportOptions, scan_media


def jpeg_with_capture_date(value):
    stamp = value.encode("ascii") + b"\x00"
    tiff = b"II" + struct.pack("<HI", 42, 8)
    tiff += struct.pack("<HHHIII", 1, 0x8769, 4, 1, 26, 0)
    tiff += struct.pack("<HHHIII", 1, 0x9003, 2, len(stamp), 44, 0) + stamp
    exif = b"Exif\x00\x00" + tiff
    return b"\xff\xd8\xff\xe1" + struct.pack(">H", len(exif) + 2) + exif + b"\xff\xd9"


class MetadataIntegrationTests(unittest.TestCase):
    def test_real_capture_metadata_controls_cutoff_and_entire_pair(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            card = root / "card"
            card.mkdir()
            (card / "PAIR.JPG").write_bytes(jpeg_with_capture_date("2026:09:20 00:00:00"))
            (card / "PAIR.NEF").write_bytes(b"raw placeholder; JPEG provides the metadata")
            (card / "PAIR.xmp").write_text("<xmp/>")
            old = datetime(2020, 1, 1).timestamp()
            for path in card.iterdir():
                os.utime(path, (old, old))
            result = scan_media(ImportOptions(card, root / "photos", cutoff=date(2026, 9, 20)))
            self.assertEqual(len(result.items), 3)
            self.assertEqual(result.fallback_count, 0)
            self.assertTrue(all(item.relative_destination.parent == Path("2026/09/20") for item in result.items))
            self.assertTrue(all(item.date_source == "capture" for item in result.items))
            later = scan_media(ImportOptions(card, root / "photos", cutoff=date(2026, 9, 21)))
            self.assertEqual(later.items, [])


if __name__ == "__main__":
    unittest.main()
