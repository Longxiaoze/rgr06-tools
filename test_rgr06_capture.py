import argparse
import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest import mock

from rgr06_capture import CaptureError, DESCRIPTOR_SHA256, _duration, _hid_identity, decode_hid_report, discover, record


class ReportDecoderTests(unittest.TestCase):
    def test_keyboard_preserves_slots_reserved_and_modifier_bits(self):
        raw = bytes([1, 0x81, 0xA5, 4, 5, 0, 0, 0, 0])
        result = decode_hid_report(raw, DESCRIPTOR_SHA256)
        self.assertEqual(result["hex"], raw.hex())
        self.assertEqual(result["decoded"], {
            "kind": "keyboard", "modifier_bits": 0x81, "reserved": 0xA5,
            "modifier_usages": [0xE0, 0xE7], "usage_slots": [4, 5, 0, 0, 0, 0],
        })

    def test_consumer_is_little_endian_and_preserves_release_zero(self):
        self.assertEqual(decode_hid_report(b"\x02\x34\x12", DESCRIPTOR_SHA256)["decoded"]["usage"], 0x1234)
        self.assertEqual(decode_hid_report(b"\x02\0\0", DESCRIPTOR_SHA256)["decoded"]["usage"], 0)

    def test_digitizer_field_order_flags_and_unclamped_values(self):
        raw = bytes([3, 0x83, 2]) + struct.pack("<HHHH", 4095, 0, 5, 65535)
        result = decode_hid_report(raw, DESCRIPTOR_SHA256)["decoded"]
        self.assertEqual((result["x"], result["y"], result["width"], result["height"]), (4095, 0, 5, 65535))
        self.assertTrue(result["tip_switch"])
        self.assertTrue(result["in_range"])
        self.assertEqual(result["constant_flag_bits"], 0x80)
        self.assertEqual(result["contact_count"], 2)

    def test_unknown_and_malformed_reports_keep_bytes_without_guesses(self):
        cases = [(b"", "unknown_report_id"), (b"\xff\x01", "unknown_report_id"),
                 (b"\x01\0", "unexpected_report_length"), (bytes([3]) + bytes(11), "unexpected_report_length")]
        for raw, status in cases:
            with self.subTest(raw=raw):
                result = decode_hid_report(raw, DESCRIPTOR_SHA256)
                self.assertEqual(result["hex"], raw.hex())
                self.assertIsNone(result["decoded"])
                self.assertEqual(result["decode_status"], status)
        changed = decode_hid_report(bytes([1]) + bytes(8), "different-descriptor")
        self.assertEqual(changed["decode_status"], "unknown_descriptor")
        self.assertIsNone(changed["decoded"])

    def test_identity_requires_complete_numeric_bus_vendor_product_and_unique_address(self):
        self.assertEqual(_hid_identity({"HID_ID": "0005:0000248A:0000045B", "HID_UNIQ": "D1:05:CB:5C:37:15"}),
                         (5, 0x248A, 0x045B, "d1:05:cb:5c:37:15"))
        self.assertIsNone(_hid_identity({"HID_ID": "invalid"}))
        self.assertIsNone(_hid_identity({"HID_ID": "5:248a:45b:6"}))

    def test_duration_bounds_nonfinite_and_unbounded_requests(self):
        self.assertEqual(_duration("0.5"), 0.5)
        for value in ("0", "-1", "601", "inf", "nan", "words"):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                _duration(value)


class DiscoveryTests(unittest.TestCase):
    def test_excludes_same_model_other_address_and_refuses_ambiguous_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            sysfs, dev = Path(directory) / "sys", Path(directory) / "dev"
            devices = sysfs / "bus/hid/devices"
            for dirname, address in (("chosen", "d1:05:cb:5c:37:15"), ("other", "00:11:22:33:44:55")):
                root = devices / dirname
                root.mkdir(parents=True)
                (root / "uevent").write_text(f"HID_ID=0005:0000248A:0000045B\nHID_UNIQ={address}\nHID_NAME=RGR06\n")
                (root / "report_descriptor").write_bytes(b"unknown descriptor")
                event = root / "input/input87/event99"
                event.mkdir(parents=True)
                (event.parent / "name").write_text("RGR06 Keyboard\n")
                (event / "uevent").write_text("DEVNAME=input/event99\n")
                (event / "dev").write_text("13:99\n")
            result = discover(sysfs, dev)
            self.assertEqual(result["hid_sysfs_path"], str(devices / "chosen"))
            self.assertEqual(len(result["nodes"]), 1)
            self.assertFalse(result["descriptor"]["known_decoder"])
            (devices / "other/uevent").write_text((devices / "chosen/uevent").read_text())
            with self.assertRaisesRegex(CaptureError, "found 2"):
                discover(sysfs, dev)


class CaptureLifecycleTests(unittest.TestCase):
    def _inspection(self):
        return {"descriptor": {"sha256": DESCRIPTOR_SHA256},
                "nodes": [{"kind": "hidraw", "path": "/not-a-real-device"}]}

    def test_passive_pipe_capture_streams_report_and_closes_owned_fd(self):
        # A pipe exercises the real selector/read/write path without opening any input device.
        read_fd, write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            os.write(write_fd, b"\x02\x34\x12")
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "new-evidence.jsonl"
                with mock.patch("rgr06_capture.discover", return_value=self._inspection()), \
                        mock.patch("rgr06_capture.open_verified", return_value=(read_fd, {})):
                    summary, status = record(0.01, output, raw_only=True)
                rows = [json.loads(line) for line in output.read_text().splitlines()]
                self.assertEqual(status, 0)
                self.assertEqual(summary["reason"], "duration_elapsed")
                self.assertEqual([row["kind"] for row in rows], ["capture_start", "hidraw_report", "capture_end"])
                self.assertEqual(rows[1]["hex"], "023412")
                self.assertEqual(rows[1]["decoded"]["usage"], 0x1234)
                self.assertEqual(summary["sources"][0]["records"], 1)
                with self.assertRaises(OSError):
                    os.fstat(read_fd)
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        finally:
            os.close(write_fd)

    def test_interrupt_writes_end_and_closes_owned_fd(self):
        read_fd, write_fd = os.pipe2(os.O_NONBLOCK | os.O_CLOEXEC)
        try:
            with tempfile.TemporaryDirectory() as directory:
                output = Path(directory) / "interrupted.jsonl"
                with mock.patch("rgr06_capture.discover", return_value=self._inspection()), \
                        mock.patch("rgr06_capture.open_verified", return_value=(read_fd, {})), \
                        mock.patch("rgr06_capture.selectors.DefaultSelector") as selector:
                    selector.return_value.__enter__.return_value.select.side_effect = KeyboardInterrupt
                    summary, status = record(1, output, raw_only=True)
                rows = [json.loads(line) for line in output.read_text().splitlines()]
                self.assertEqual(status, 130)
                self.assertEqual(summary["reason"], "interrupted")
                self.assertEqual(rows[-1]["kind"], "capture_end")
                with self.assertRaises(OSError):
                    os.fstat(read_fd)
        finally:
            os.close(write_fd)


if __name__ == "__main__":
    unittest.main()
