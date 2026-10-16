#!/usr/bin/env python3
"""Passive, target-bound RGR06 HID inspection/capture; Python 3 stdlib only.

No output reports, grabs, ACL changes, pairing changes, or input injection.
The descriptor decoder is pinned to the descriptor inspected on this device.
"""

import argparse
from contextlib import ExitStack
import datetime
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import selectors
import signal
import stat
import struct
import sys
import time


TARGET_BUS = 0x0005
TARGET_VENDOR = 0x248A
TARGET_PRODUCT = 0x045B
TARGET_UNIQ = "d1:05:cb:5c:37:15"
DESCRIPTOR_SHA256 = "177ec423b4adb8fcd2137ed9476f5d61baa261950aa93b9cb33d25e7f4be02e6"
INPUT_EVENT = struct.Struct("@llHHi")
RAW_DESCRIPTOR_MAX = 4096
MAX_SECONDS = 600


class CaptureError(Exception):
    """An expected discovery, access, or identity failure."""


def _uevent(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)


def _hid_identity(properties):
    parts = properties.get("HID_ID", "").split(":")
    try:
        identity = tuple(int(part, 16) for part in parts)
    except ValueError:
        return None
    if len(identity) != 3:
        return None
    return identity + (properties.get("HID_UNIQ", "").lower(),)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def discover(sysfs_root=Path("/sys"), dev_root=Path("/dev")):
    """Select one HID by bus/vendor/product/unique address, never its event number."""
    expected = (TARGET_BUS, TARGET_VENDOR, TARGET_PRODUCT, TARGET_UNIQ)
    matches = []
    for entry in (sysfs_root / "bus/hid/devices").glob("*"):
        try:
            properties = _uevent(entry / "uevent")
        except FileNotFoundError:
            continue
        if _hid_identity(properties) == expected:
            matches.append((entry.resolve(strict=True), properties))
    if len(matches) != 1:
        raise CaptureError(f"Expected exactly one RGR06 with identity {expected}; found {len(matches)}")
    root, properties = matches[0]
    descriptor = (root / "report_descriptor").read_bytes()
    digest = hashlib.sha256(descriptor).hexdigest()
    nodes = []
    for kind, pattern in (("evdev", "input/input*/event*"), ("hidraw", "hidraw/hidraw*")):
        for node in sorted(root.glob(pattern)):
            node_props = _uevent(node / "uevent")
            devname = node_props.get("DEVNAME", "")
            relative = Path(devname)
            if not devname or relative.is_absolute() or ".." in relative.parts:
                raise CaptureError(f"Unexpected sysfs DEVNAME at {node}")
            devpath = dev_root / relative
            major, minor = (int(part) for part in (node / "dev").read_text().strip().split(":"))
            try:
                node_stat = devpath.stat()
                mode = stat.filemode(node_stat.st_mode)
                exists = True
            except FileNotFoundError:
                mode = None
                exists = False
            name_path = node.parent / "name" if kind == "evdev" else root / "uevent"
            name = name_path.read_text().strip() if kind == "evdev" else properties.get("HID_NAME")
            nodes.append({
                "kind": kind, "path": str(devpath), "sysfs_path": str(node),
                "major": major, "minor": minor, "name": name,
                "exists": exists, "mode": mode,
                "readable": os.access(devpath, os.R_OK, effective_ids=True),
            })
    return {
        "schema": "rgr06-inspect-v1", "observed_at_utc": _utc_now(),
        "identity": {"bus": TARGET_BUS, "vendor": TARGET_VENDOR, "product": TARGET_PRODUCT,
                     "uniq": properties["HID_UNIQ"], "name": properties.get("HID_NAME"),
                     "physical": properties.get("HID_PHYS")},
        "hid_sysfs_path": str(root), "hid_uevent": properties,
        "descriptor": {"path": str(root / "report_descriptor"), "byte_length": len(descriptor),
                       "hex": descriptor.hex(), "sha256": digest,
                       "known_decoder": digest == DESCRIPTOR_SHA256},
        "nodes": nodes,
        "limitations": ["No physical-control mapping has been observed by this inspection.",
                        "Read access does not establish an observed input event.",
                        "Device numbers and access can change after reconnecting."],
    }


def decode_hid_report(report, descriptor_sha256):
    """Preserve every byte; interpret only exact report lengths under the pinned descriptor."""
    result = {"hex": report.hex(), "byte_length": len(report),
              "report_id": report[0] if report else None, "decoded": None}
    if descriptor_sha256 != DESCRIPTOR_SHA256:
        result["decode_status"] = "unknown_descriptor"
        return result
    lengths = {1: 9, 2: 3, 3: 11}
    report_id = result["report_id"]
    if report_id not in lengths:
        result["decode_status"] = "unknown_report_id"
        return result
    if len(report) != lengths[report_id]:
        result.update(decode_status="unexpected_report_length", expected_byte_length=lengths[report_id])
        return result
    if report_id == 1:
        result["decoded"] = {
            "kind": "keyboard", "modifier_bits": report[1], "reserved": report[2],
            "modifier_usages": [0xE0 + bit for bit in range(8) if report[1] & (1 << bit)],
            "usage_slots": list(report[3:]),
        }
    elif report_id == 2:
        result["decoded"] = {"kind": "consumer", "usage": struct.unpack_from("<H", report, 1)[0]}
    else:
        x, y, width, height = struct.unpack_from("<HHHH", report, 3)
        result["decoded"] = {
            "kind": "digitizer", "flags": report[1],
            "tip_switch": bool(report[1] & 1), "in_range": bool(report[1] & 2),
            "constant_flag_bits": report[1] & 0xFC, "contact_count": report[2],
            "x": x, "y": y, "width": width, "height": height,
        }
    result["decode_status"] = "decoded_from_pinned_descriptor"
    return result


def _ior(kind, number, size):
    # Linux asm-generic ioctl layout (used by this x86-64 Linux host).
    return (2 << 30) | (size << 16) | (ord(kind) << 8) | number


def _ioctl_bytes(fd, request, size):
    buffer = bytearray(size)
    returned = fcntl.ioctl(fd, request, buffer, True)
    return bytes(buffer[:returned] if returned > 0 else buffer)


def _ioctl_text(fd, kind, number):
    data = _ioctl_bytes(fd, _ior(kind, number, 256), 256)
    return data.split(b"\0", 1)[0].decode("utf-8", "strict")


def open_verified(node, inspection):
    """Open only the discovered node, then prove identity on the actual open FD."""
    fd = os.open(node["path"], os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISCHR(opened.st_mode) or opened.st_rdev != os.makedev(node["major"], node["minor"]):
            raise CaptureError(f"Device number/type changed for {node['path']}")
        if node["kind"] == "evdev":
            bus, vendor, product, version = struct.unpack("@HHHH", _ioctl_bytes(fd, _ior("E", 2, 8), 8))
            uniq = _ioctl_text(fd, "E", 8)
            name = _ioctl_text(fd, "E", 6)
        else:
            bus, vendor, product = struct.unpack("@IHH", _ioctl_bytes(fd, _ior("H", 3, 8), 8))
            version = None
            uniq = _ioctl_text(fd, "H", 8)
            name = _ioctl_text(fd, "H", 4)
        if (bus, vendor, product, uniq.lower()) != (TARGET_BUS, TARGET_VENDOR, TARGET_PRODUCT, TARGET_UNIQ):
            raise CaptureError(f"Opened device identity does not match the selected RGR06: {node['path']}")
        if node["kind"] == "hidraw":
            desc_size = struct.unpack("@I", _ioctl_bytes(fd, _ior("H", 1, 4), 4))[0]
            if not 0 < desc_size <= RAW_DESCRIPTOR_MAX:
                raise CaptureError(f"Invalid raw descriptor size {desc_size}")
            buffer = bytearray(struct.pack("@I", desc_size) + bytes(RAW_DESCRIPTOR_MAX))
            fcntl.ioctl(fd, _ior("H", 2, len(buffer)), buffer, True)
            returned_size = struct.unpack_from("@I", buffer)[0]
            if returned_size != desc_size:
                raise CaptureError("Opened HID descriptor length changed")
            digest = hashlib.sha256(buffer[4:4 + desc_size]).hexdigest()
            if digest != inspection["descriptor"]["sha256"]:
                raise CaptureError("Opened HID descriptor differs from inspected descriptor")
        current_sysfs = Path(node["sysfs_path"])
        if _hid_identity(_uevent(Path(inspection["hid_sysfs_path"]) / "uevent")) != (
                TARGET_BUS, TARGET_VENDOR, TARGET_PRODUCT, TARGET_UNIQ):
            raise CaptureError("Selected HID sysfs identity changed")
        if (current_sysfs / "dev").read_text().strip() != f"{node['major']}:{node['minor']}":
            raise CaptureError("Selected sysfs node device number changed")
        return fd, {"bus": bus, "vendor": vendor, "product": product,
                    "version": version, "uniq": uniq, "name": name,
                    "major": os.major(opened.st_rdev), "minor": os.minor(opened.st_rdev)}
    except BaseException:
        os.close(fd)
        raise


def _write_jsonl(stream, value):
    stream.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
    stream.flush()


def record(seconds, output, raw_only=False):
    inspection = discover()
    nodes = [node for node in inspection["nodes"] if not raw_only or node["kind"] == "hidraw"]
    if not nodes or not any(node["kind"] == "hidraw" for node in nodes):
        raise CaptureError("No matching hidraw node was found")
    if not raw_only and not any(node["kind"] == "evdev" for node in nodes):
        raise CaptureError("No matching evdev nodes were found; --raw-only selects only hidraw")
    with ExitStack() as stack:
        selector = stack.enter_context(selectors.DefaultSelector())
        verified = []
        for node in nodes:
            try:
                fd, identity = open_verified(node, inspection)
            except PermissionError as error:
                raise CaptureError(f"Read permission denied for selected RGR06 node {node['path']}; "
                                   "no capture started and no permissions were changed") from error
            stack.callback(os.close, fd)
            source = {**node, "opened_identity": identity, "pending": bytearray(), "records": 0}
            verified.append(source)
            selector.register(fd, selectors.EVENT_READ, source)
        # Exclusive creation preserves existing evidence. All selected FDs must verify first.
        output_fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        stream = stack.enter_context(os.fdopen(output_fd, "w", encoding="utf-8"))
        started = time.monotonic_ns()
        _write_jsonl(stream, {
            "kind": "capture_start", "schema": "rgr06-capture-v1", "at_utc": _utc_now(),
            "received_monotonic_ns": started, "requested_seconds": seconds,
            "requested_output": str(output), "output_absolute_path": os.path.abspath(output),
            "raw_only": raw_only, "passive": True, "input_event_struct_size": INPUT_EVENT.size,
            "inspection": inspection,
            "opened_sources": [{key: value for key, value in source.items() if key not in ("pending", "records")}
                               for source in verified],
        })
        deadline = started + int(seconds * 1_000_000_000)
        reason, interrupted, failure = "duration_elapsed", False, None
        previous_term_handler = signal.getsignal(signal.SIGTERM)
        def interrupt(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGTERM, interrupt)
        try:
            while True:
                remaining = (deadline - time.monotonic_ns()) / 1_000_000_000
                if remaining <= 0:
                    break
                for key, _ in selector.select(remaining):
                    if time.monotonic_ns() >= deadline:
                        break
                    source = key.data
                    try:
                        data = os.read(key.fd, RAW_DESCRIPTOR_MAX if source["kind"] == "hidraw" else INPUT_EVENT.size * 64)
                    except BlockingIOError:
                        continue
                    received = {"received_monotonic_ns": time.monotonic_ns(), "received_wall_time_ns": time.time_ns()}
                    if not data:
                        raise CaptureError(f"Device disconnected: {source['path']}")
                    if source["kind"] == "hidraw":
                        _write_jsonl(stream, {"kind": "hidraw_report", "source": source["path"], **received,
                                            **decode_hid_report(data, inspection["descriptor"]["sha256"])})
                        source["records"] += 1
                    else:
                        source["pending"].extend(data)
                        while len(source["pending"]) >= INPUT_EVENT.size:
                            raw = bytes(source["pending"][:INPUT_EVENT.size])
                            del source["pending"][:INPUT_EVENT.size]
                            sec, usec, event_type, code, value = INPUT_EVENT.unpack(raw)
                            _write_jsonl(stream, {"kind": "evdev_event", "source": source["path"], **received,
                                                "kernel_timeval_seconds": sec, "kernel_timeval_microseconds": usec,
                                                "type": event_type, "code": code, "value": value, "hex": raw.hex()})
                            source["records"] += 1
        except KeyboardInterrupt:
            reason, interrupted = "interrupted", True
        except (OSError, CaptureError) as error:
            reason, failure = "capture_error", str(error)
        finally:
            signal.signal(signal.SIGTERM, previous_term_handler)
            ended = time.monotonic_ns()
            summary = {
                "kind": "capture_end", "at_utc": _utc_now(), "received_monotonic_ns": ended,
                "elapsed_seconds": (ended - started) / 1_000_000_000, "reason": reason,
                "error": failure,
                "sources": [{"path": source["path"], "records": source["records"],
                             "unparsed_trailing_hex": source["pending"].hex()} for source in verified],
            }
            _write_jsonl(stream, summary)
            os.fsync(stream.fileno())
        return summary, 130 if interrupted else (2 if failure else 0)


def _duration(value):
    try:
        number = float(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("seconds must be numeric") from error
    if not math.isfinite(number) or not 0 < number <= MAX_SECONDS:
        raise argparse.ArgumentTypeError(f"seconds must be finite and greater than 0, at most {MAX_SECONDS}")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect", help="Inspect only the selected RGR06 sysfs identity, descriptor, and access")
    capture = commands.add_parser("record", help="Passively record only verified RGR06 nodes; never grab them")
    capture.add_argument("--seconds", required=True, type=_duration, help=f"Capture duration, 0 < seconds <= {MAX_SECONDS}")
    capture.add_argument("--output", required=True, help="New JSONL path; an existing file is never overwritten")
    capture.add_argument("--raw-only", action="store_true", help="Open only matching hidraw; no evdev access needed")
    args = parser.parse_args(argv)
    try:
        if args.command == "inspect":
            print(json.dumps(discover(), ensure_ascii=False, indent=2))
            return 0
        summary, code = record(args.seconds, args.output, args.raw_only)
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return code
    except (CaptureError, OSError, UnicodeError, ValueError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
