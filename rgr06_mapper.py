#!/usr/bin/env python3
"""Foreground, target-bound RGR06 -> one touchscreen plus one mouse.

Python 3 standard library only. Explicit --run is required to grab the selected
RGR06 inputs and create virtual devices. There are no built-in button guesses.
The passive capture module owns discovery, opened-FD identity, and HID parsing.
"""

import argparse
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import select
import signal
import stat
import struct
import sys
import time

from rgr06_capture import (
    CaptureError, DESCRIPTOR_SHA256, TARGET_BUS, TARGET_VENDOR, TARGET_PRODUCT, TARGET_UNIQ, decode_hid_report,
    discover, open_verified,
)
from rgr06_motion import MotionError, RelativeTouchMotion, validate_gain


EV_SYN, EV_KEY, EV_REL, EV_ABS = 0, 1, 2, 3
SYN_REPORT = 0
REL_X, REL_Y, REL_WHEEL = 0, 1, 8
ABS_X, ABS_Y = 0, 1
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE, BTN_TOUCH = 0x110, 0x111, 0x112, 0x14A
INPUT_PROP_DIRECT = 1
EVENT = struct.Struct("@llHHi")
ACTIONS = {"left": BTN_LEFT, "right": BTN_RIGHT, "middle": BTN_MIDDLE}
WHEELS = {"wheel_up": 1, "wheel_down": -1}
CONTROLS = ("left", "right", "wheel_press", "wheel_forward", "wheel_backward")
MACRO_TIMEOUT_SECONDS = 1.0
MOUSE_EDGE_INTERVAL_SECONDS = 0.030


class MapperError(Exception):
    pass


class MappingStopped(Exception):
    """Stop requested while waiting to emit a normal mouse edge."""


def _iow(kind, number, size):
    return (1 << 30) | (size << 16) | (ord(kind) << 8) | number


EVIOCGRAB = _iow("E", 0x90, 4)
UI_DEV_CREATE, UI_DEV_DESTROY = (ord("U") << 8) | 1, (ord("U") << 8) | 2
UI_DEV_SETUP, UI_ABS_SETUP = _iow("U", 3, 92), _iow("U", 4, 28)
UI_SET_EVBIT, UI_SET_KEYBIT = _iow("U", 100, 4), _iow("U", 101, 4)
UI_SET_RELBIT, UI_SET_ABSBIT, UI_SET_PROPBIT = (
    _iow("U", 102, 4), _iow("U", 103, 4), _iow("U", 110, 4)
)


def _integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise MapperError(f"{label} must be an integer in {minimum}..{maximum}")
    return value


def _keys(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise MapperError(f"Unexpected fields or non-object at {label}")


@dataclass(frozen=True)
class Binding:
    report_id: int
    usage: int
    modifiers: int
    action: str


@dataclass(frozen=True)
class Config:
    bindings: tuple
    touch_enabled: bool = True
    swap_xy: bool = False
    invert_x: bool = False
    invert_y: bool = False
    contact_signal: str = "tip_switch"
    profile: str = "raw"
    controls: tuple = ()
    touch_mode: str = "pointer"
    touch_gain: float = 0.5


def parse_config(value):
    _keys(value, ("schema_version", "device", "touch", "bindings", "profile", "controls"), "config")
    if type(value.get("schema_version")) is not int or value["schema_version"] != 1:
        raise MapperError("schema_version must be 1")
    device = value.get("device")
    _keys(device, ("uniq",), "device")
    uniq = device.get("uniq")
    if not isinstance(uniq, str) or uniq.lower() != TARGET_UNIQ:
        raise MapperError(f"Config device.uniq must identify {TARGET_UNIQ}")
    touch = value.get("touch", {})
    _keys(touch, ("enabled", "swap_xy", "invert_x", "invert_y", "contact_signal", "mode", "gain"), "touch")
    for key, field in touch.items():
        if key not in ("contact_signal", "mode", "gain") and type(field) is not bool:
            raise MapperError(f"touch.{key} must be boolean")
    contact_signal = touch.get("contact_signal", "tip_switch")
    if contact_signal not in ("tip_switch", "in_range"):
        raise MapperError("touch.contact_signal must be tip_switch or in_range")
    touch_mode = touch.get("mode", "pointer")
    if touch_mode not in ("pointer", "touchscreen"):
        raise MapperError("touch.mode must be pointer or touchscreen")
    touch_gain = validate_gain(touch.get("gain", 0.5))
    profile = value.get("profile", "raw")
    if profile not in ("raw", "rgr06-observed-v1"):
        raise MapperError("profile must be raw or rgr06-observed-v1")
    controls = value.get("controls", {})
    _keys(controls, CONTROLS, "controls")
    for action in controls.values():
        if not isinstance(action, str) or action not in (ACTIONS | WHEELS):
            raise MapperError("Each control maps to left, right, middle, wheel_up, or wheel_down")
    values = value.get("bindings", [])
    if not isinstance(values, list) or len(values) > 64:
        raise MapperError("bindings must be an array with at most 64 entries")
    if profile == "raw" and controls:
        raise MapperError("controls require the rgr06-observed-v1 profile")
    if profile == "rgr06-observed-v1" and values:
        raise MapperError("Observed firmware controls and raw bindings cannot be active together")
    bindings, seen = [], set()
    for index, item in enumerate(values):
        _keys(item, ("source", "action"), f"bindings[{index}]")
        source = item.get("source")
        _keys(source, ("report_id", "usage", "modifiers"), f"bindings[{index}].source")
        report_id = _integer(source.get("report_id"), 1, 2, "source.report_id")
        usage = _integer(source.get("usage"), 4 if report_id == 1 else 1,
                         0xF1 if report_id == 1 else 0x28C, "source.usage")
        modifiers = _integer(source.get("modifiers", 0), 0, 255, "source.modifiers")
        if report_id == 2 and "modifiers" in source:
            raise MapperError("Consumer sources do not have keyboard modifiers")
        if report_id == 1 and 0xE0 <= usage <= 0xE7:
            raise MapperError("Modifier keys belong in source.modifiers, not source.usage")
        action = item.get("action")
        if not isinstance(action, str) or action not in (ACTIONS | WHEELS):
            raise MapperError("action must be left, right, middle, wheel_up, or wheel_down")
        identity = (report_id, usage, modifiers)
        if identity in seen:
            raise MapperError("Duplicate source mapping; one source can have only one action")
        seen.add(identity)
        bindings.append(Binding(*identity, action))
    return Config(tuple(bindings), touch.get("enabled", True), touch.get("swap_xy", False),
                  touch.get("invert_x", False), touch.get("invert_y", False), contact_signal,
                  profile, tuple(controls.items()), touch_mode, touch_gain)


def load_config(path):
    with path.open(encoding="utf-8") as source:
        content = source.read(65537)
    if len(content) > 65536:
        raise MapperError("Config exceeds 64 KiB")
    return parse_config(json.loads(content))


def decoded_report(raw):
    report = decode_hid_report(raw, DESCRIPTOR_SHA256)
    if report["decode_status"] != "decoded_from_pinned_descriptor":
        raise MapperError(f"Cannot safely map {report['decode_status']}: {report['hex']}")
    result = report["decoded"]
    if result["kind"] == "keyboard":
        if any(0 < usage < 4 or usage > 0xF1 for usage in result["usage_slots"]):
            raise MapperError("Keyboard rollover/error or out-of-descriptor usage")
    elif result["kind"] == "consumer":
        # Zero is the observed HID array no-selection/release convention.
        if result["usage"] > 0x28C:
            raise MapperError("Consumer usage is outside the pinned descriptor range")
    elif result["kind"] == "digitizer":
        if result["contact_count"] > 1:
            raise MapperError("Multiple contacts are outside the pinned single-contact descriptor")
        if result["tip_switch"] and not (0 <= result["x"] <= 4095 and 0 <= result["y"] <= 4095):
            raise MapperError("Active touch coordinates are outside 0..4095")
    return result


class FirmwareActions:
    """Owns the observed firmware pulse grammar; F1 is never an action.

    Tokens are complete single-usage HID down/up pairs. Button recognition is
    limited to captured sequences. A wake plus a direction starts a wheel
    burst; each direction pulse is a logical scroll unit, not a claimed
    physical detent count. UP/DOWN close the burst without adding a unit.
    A one-second idle bound exceeds the observed 0.383-second maximum gap.
    Call expire only after draining already-readable raw packets during live
    operation; replay callers can use the captured receive timestamps.
    """

    SEQUENCES = {
        ((1, 0x3C),): ("left",),
        ((1, 0x3A), (1, 0x28)): ("right",),
        ((1, 0x3A), (1, 0x3D)): ("wheel_press",),
        ((1, 0x3A), (1, 0x3A), (2, 0x224)): ("right", "right"),
        ((1, 0x3A), (1, 0x3A), (1, 0x3E)): ("wheel_press", "wheel_press"),
    }
    WHEEL_STARTS = {
        ((1, 0x3A), (1, 0x50)): "wheel_forward",
        ((1, 0x3A), (1, 0x4F)): "wheel_backward",
    }
    WHEEL_DIRECTIONS = {(1, 0x50): "wheel_forward", (1, 0x4F): "wheel_backward"}
    WHEEL_ENDS = {(1, 0x52), (1, 0x51)}

    def __init__(self):
        self.prefix = []
        self.pressed = {}
        self.last_token_at = None
        self.counts = Counter()
        self.recognized = Counter()
        self.wheel_active = False

    def _drop_pending(self):
        if self.prefix:
            self.counts["incomplete_sequences"] += 1
            self.counts["dropped_taps"] += len(self.prefix)
            self.prefix.clear()
        if self.wheel_active:
            self.counts["incomplete_wheel_bursts"] += 1
            self.wheel_active = False
        self.last_token_at = None

    def expire(self, now):
        if self.last_token_at is not None and now - self.last_token_at >= MACRO_TIMEOUT_SECONDS:
            self._drop_pending()
        for report_id, (usage, started) in list(self.pressed.items()):
            if usage is not None and now - started >= MACRO_TIMEOUT_SECONDS:
                # Timeout abandons this tap, but does not establish release.
                # Keep quarantine until the source reports actual neutral.
                self.pressed[report_id] = (None, now)
                self.counts["incomplete_taps"] += 1
                self._drop_pending()

    def finish(self):
        self._drop_pending()
        self.counts["incomplete_taps"] += sum(usage is not None for usage, _ in self.pressed.values())
        self.pressed.clear()

    def feed(self, report, now):
        if report["kind"] == "digitizer":
            return ()
        report_id = 1 if report["kind"] == "keyboard" else 2
        if report_id == 1:
            usages = set(report["usage_slots"]) - {0}
            if report["modifier_bits"] or report["reserved"] or len(usages) > 1:
                self.counts["unknown_states"] += 1
                self.pressed[report_id] = (None, now)
                self._drop_pending()
                return ()
            usage = next(iter(usages), 0)
        else:
            usage = report["usage"]
        previous = self.pressed.get(report_id)
        if usage:
            if previous is not None and previous[0] is None:
                return ()  # malformed/chord state must become neutral first
            if any(other_id != report_id for other_id in self.pressed):
                self.counts["overlapping_report_states"] += 1
                self.pressed = {other_id: (None, now) for other_id in self.pressed}
                self.pressed[report_id] = (None, now)
                self._drop_pending()
                return ()
            if previous is None:
                self.pressed[report_id] = (usage, now)
            elif previous[0] != usage:
                self.counts["malformed_transitions"] += 1
                self.pressed[report_id] = (None, now)
                self._drop_pending()
            return ()
        if previous is None:
            return ()  # duplicate neutral packets do not complete any tap
        del self.pressed[report_id]
        if previous[0] is None:
            return ()
        token = (report_id, previous[0])
        if self.wheel_active:
            if token in self.WHEEL_DIRECTIONS:
                action = self.WHEEL_DIRECTIONS[token]
                self.last_token_at = now
                self.recognized[action] += 1
                return (action,)
            if token in self.WHEEL_ENDS:
                # A captured mixed-direction burst ended in DOWN after LEFT;
                # the terminator does not establish the direction or a unit.
                self.wheel_active = False
                self.last_token_at = None
                return ()
            self._drop_pending()
        self.prefix.append(token)
        self.last_token_at = now
        candidate = tuple(self.prefix)
        if candidate in self.SEQUENCES:
            actions = self.SEQUENCES[candidate]
            self.recognized.update(actions)
            self.prefix.clear()
            self.last_token_at = None
            return actions
        if candidate in self.WHEEL_STARTS:
            action = self.WHEEL_STARTS[candidate]
            self.prefix.clear()
            self.wheel_active = True
            self.recognized[action] += 1
            return (action,)
        if any(sequence[:len(candidate)] == candidate for sequence in (*self.SEQUENCES, *self.WHEEL_STARTS)):
            return ()
        self.counts["unknown_sequences"] += 1
        self.counts["dropped_taps"] += len(self.prefix)
        self.prefix.clear()
        self.last_token_at = None
        return ()

    def statistics(self):
        return {"recognized_controls": dict(self.recognized), "drops": dict(self.counts),
                "pending_taps": len(self.prefix), "pressed_report_states": len(self.pressed),
                "wheel_burst_active": self.wheel_active,
                "sequence_idle_timeout_seconds": MACRO_TIMEOUT_SECONDS}


class MappingEngine:
    """Tracks independent HID report states and aggregate virtual button owners."""

    def __init__(self, config, mouse_frame, touch_frame):
        self.config, self.mouse_frame, self.touch_frame = config, mouse_frame, touch_frame
        self.active_sources = set()
        self.buttons_down = set()
        self.touch_down = False
        self.firmware = FirmwareActions() if config.profile == "rgr06-observed-v1" else None
        self.controls = dict(config.controls)
        self.relative_touch = RelativeTouchMotion(
            contact_signal=config.contact_signal, gain=config.touch_gain,
            swap_xy=config.swap_xy, invert_x=config.invert_x, invert_y=config.invert_y,
        ) if config.touch_mode == "pointer" and config.touch_enabled else None

    def feed(self, raw, now=None):
        report = decoded_report(raw)
        if report["kind"] == "digitizer":
            if self.config.touch_enabled:
                self._touch(report)
            return
        if self.firmware is not None:
            for control in self.firmware.feed(report, time.monotonic() if now is None else now):
                self._control(control)
            return
        report_id = 1 if report["kind"] == "keyboard" else 2
        current = set()
        for binding in self.config.bindings:
            if binding.report_id != report_id:
                continue
            if report_id == 1:
                active = (binding.modifiers == report["modifier_bits"] and
                          binding.usage in report["usage_slots"])
            else:
                active = binding.usage == report["usage"]
            if active:
                current.add(binding)
        previous = {binding for binding in self.active_sources if binding.report_id == report_id}
        self.active_sources.difference_update(previous)
        self.active_sources.update(current)
        desired = {ACTIONS[binding.action] for binding in self.active_sources if binding.action in ACTIONS}
        events = [(EV_KEY, button, 0) for button in sorted(self.buttons_down - desired)]
        events += [(EV_KEY, button, 1) for button in sorted(desired - self.buttons_down)]
        events += [(EV_REL, REL_WHEEL, WHEELS[binding.action])
                   for binding in sorted(current - previous, key=lambda b: (b.usage, b.modifiers))
                   if binding.action in WHEELS]
        # Conservatively retain possible downs if a write fails partway through.
        self.buttons_down.update(desired)
        if events:
            self.mouse_frame(events)
        self.buttons_down = desired

    def _control(self, control):
        action = self.controls.get(control)
        if action in WHEELS:
            self.mouse_frame([(EV_REL, REL_WHEEL, WHEELS[action])])
        elif action in ACTIONS:
            button = ACTIONS[action]
            self.buttons_down.add(button)
            self.mouse_frame([(EV_KEY, button, 1)])
            self.mouse_frame([(EV_KEY, button, 0)])
            self.buttons_down.discard(button)

    def expire(self, now):
        if self.firmware is not None:
            self.firmware.expire(now)

    def statistics(self):
        result = self.firmware.statistics() if self.firmware is not None else {"profile": "raw"}
        if self.relative_touch is not None:
            result["relative_touch"] = self.relative_touch.statistics()
        return result

    def _touch(self, report):
        if self.relative_touch is not None:
            events = self.relative_touch.feed(report)
            if events:
                self.mouse_frame(events)
            return
        down = report[self.config.contact_signal]
        if not down:
            if self.touch_down:
                self.touch_frame([(EV_KEY, BTN_TOUCH, 0)])
                self.touch_down = False
            return
        x, y = report["x"], report["y"]
        if not (0 <= x <= 4095 and 0 <= y <= 4095):
            raise MapperError("Active adapted touch coordinates are outside 0..4095")
        if self.config.swap_xy:
            x, y = y, x
        if self.config.invert_x:
            x = 4095 - x
        if self.config.invert_y:
            y = 4095 - y
        self.touch_down = True
        # BTN_TOUCH first is required for legacy mousedev frame compatibility.
        self.touch_frame([(EV_KEY, BTN_TOUCH, 1), (EV_ABS, ABS_X, x), (EV_ABS, ABS_Y, y)])

    def release_all(self):
        if self.relative_touch is not None:
            self.relative_touch.reset()
        if self.firmware is not None:
            self.firmware.finish()
        failures = []
        if self.buttons_down:
            try:
                self.mouse_frame([(EV_KEY, button, 0) for button in sorted(self.buttons_down)])
                self.buttons_down.clear()
            except (OSError, MapperError) as error:
                failures.append(error)
        if self.touch_down:
            try:
                self.touch_frame([(EV_KEY, BTN_TOUCH, 0)])
                self.touch_down = False
            except (OSError, MapperError) as error:
                failures.append(error)
        self.active_sources.clear()
        if failures:
            raise failures[0]


def _uinput_fd():
    expected = Path("/sys/class/misc/uinput/dev").read_text().strip()
    major, minor = (int(part) for part in expected.split(":"))
    fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISCHR(opened.st_mode) or opened.st_rdev != os.makedev(major, minor):
            raise MapperError("Opened /dev/uinput identity changed")
    except BaseException:
        os.close(fd)
        raise
    return fd


class VirtualInput:
    def __init__(self, kind):
        self.fd, self.created = _uinput_fd(), False
        try:
            fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
            if kind == "touch":
                fcntl.ioctl(self.fd, UI_SET_KEYBIT, BTN_TOUCH)
                fcntl.ioctl(self.fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_ABS)
                for axis in (ABS_X, ABS_Y):
                    fcntl.ioctl(self.fd, UI_SET_ABSBIT, axis)
                    fcntl.ioctl(self.fd, UI_ABS_SETUP, struct.pack("@H2xiiiiii", axis, 0, 0, 4095, 0, 0, 0))
                name = b"RGR06 mapped touchscreen"
            elif kind == "mouse":
                for button in ACTIONS.values():
                    fcntl.ioctl(self.fd, UI_SET_KEYBIT, button)
                fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
                for axis in (REL_X, REL_Y, REL_WHEEL):
                    fcntl.ioctl(self.fd, UI_SET_RELBIT, axis)
                name = b"RGR06 mapped mouse"
            else:
                raise MapperError("Unknown virtual input kind")
            # BUS_VIRTUAL and distinct IDs prevent selection as the physical RGR06.
            fcntl.ioctl(self.fd, UI_DEV_SETUP, struct.pack("@HHHH80sI", 6, 0, 0, 1, name, 0))
            fcntl.ioctl(self.fd, UI_DEV_CREATE)
            self.created = True
        except BaseException:
            self.close()
            raise

    def frame(self, events):
        payload = b"".join(EVENT.pack(0, 0, event_type, code, value)
                           for event_type, code, value in [*events, (EV_SYN, SYN_REPORT, 0)])
        deadline = time.monotonic() + 1
        while payload:
            try:
                written = os.write(self.fd, payload)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MapperError("Timed out writing virtual input frame")
                select.select([], [self.fd], [], min(0.05, max(0, deadline - time.monotonic())))
                continue
            if written <= 0 or written % EVENT.size:
                raise MapperError("Incomplete virtual input event write")
            payload = payload[written:]

    def close(self):
        if self.fd is None:
            return
        fd, self.fd = self.fd, None
        try:
            if self.created:
                fcntl.ioctl(fd, UI_DEV_DESTROY)
        finally:
            os.close(fd)


class PacedMouse:
    """Separate real button edges so libinput does not merge double clicks.

    The first edge is immediate. Cleanup disables pacing before releasing the
    engine's conservative pressed-state ledger. Pure report replay bypasses
    this real-output adapter entirely.
    """

    def __init__(self, emit, should_stop=lambda: False, *, clock=time.monotonic, wait=time.sleep):
        self.emit, self.should_stop, self.clock, self.wait = emit, should_stop, clock, wait
        self.last_edge = None
        self.pacing = True

    def disable(self):
        self.pacing = False

    def _wait_edge(self):
        while self.pacing:
            if self.should_stop():
                raise MappingStopped()
            if self.last_edge is None:
                return
            remaining = self.last_edge + MOUSE_EDGE_INTERVAL_SECONDS - self.clock()
            if remaining <= 0:
                return
            # A stop-only signal handler can interrupt normal output without
            # deferring final release for a whole remaining click interval.
            self.wait(min(remaining, 0.005))

    def frame(self, events):
        if not self.pacing:
            self.emit(events)
            return
        pending = []
        for event in events:
            event_type, code, _ = event
            if event_type == EV_KEY and code in ACTIONS.values():
                self._wait_edge()
                self.emit([*pending, event])
                pending.clear()
                self.last_edge = self.clock()
            else:
                pending.append(event)
        if pending:
            self.emit(pending)


def release_engine(engine, mouse_output):
    mouse_output.disable()
    engine.release_all()


class CleanupLedger:
    """Complete every cleanup, and never report a failed finalization as success."""

    def __init__(self):
        self.errors = []

    def __enter__(self):
        return self

    def run(self, function, *args):
        try:
            function(*args)
        except Exception as error:
            self.errors.append(f"{function.__name__}: {error}")

    def __exit__(self, exception_type, exception, traceback):
        if self.errors:
            message = "Cleanup failed: " + "; ".join(self.errors)
            if exception_type is None:
                raise MapperError(message)
            print(message, file=sys.stderr)
        return False


def _lock(stack):
    path = Path(__file__).resolve().with_name(".rgr06-mapper.lock")
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    stack.callback(os.close, fd)
    if os.fstat(fd).st_uid != os.geteuid() or not stat.S_ISREG(os.fstat(fd).st_mode):
        raise MapperError("Mapper lock is not an owned regular file")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        raise MapperError("Another mapper instance already owns the lock") from error


def _first_report(fd, seconds, should_stop=lambda: False):
    deadline = time.monotonic() + seconds
    while not should_stop() and time.monotonic() < deadline:
        if not select.select([fd], [], [], min(0.25, max(0, deadline - time.monotonic())))[0]:
            continue
        try:
            raw = os.read(fd, 4096)
        except BlockingIOError:
            continue
        if not raw:
            raise MapperError("RGR06 disconnected during preflight")
        decoded_report(raw)
        return raw
    return None


def _drain(fd):
    for _ in range(256):
        try:
            raw = os.read(fd, 65536)
        except BlockingIOError:
            return
        if not raw:
            raise MapperError("RGR06 disconnected")
    raise MapperError("RGR06 input queue did not become idle within the bounded startup drain")


def _require_released(evdev_fds):
    # Check actual kernel input state, including tool proximity, rather than
    # guessing physical state from an arbitrary HID packet. This cannot make
    # two separate evdev grabs atomic; keep controls released until active.
    if not _keys_released(evdev_fds):
        raise MapperError("RGR06 controls are active; release buttons and move your finger away, then rerun")


def _keys_released(evdev_fds):
    request = (2 << 30) | (128 << 16) | (ord("E") << 8) | 0x18
    for fd in evdev_fds:
        keys = bytearray(128)
        fcntl.ioctl(fd, request, keys, True)
        if any(keys):
            return False
    return True


def _wait_released(evdev_fds, deadline, should_stop):
    while not should_stop() and time.monotonic() < deadline:
        if _keys_released(evdev_fds):
            return True
        select.select([], [], [], min(0.05, max(0, deadline - time.monotonic())))
    return False


def operate(config, *, check, seconds, arm_timeout=30):
    if sys.platform != "linux" or os.uname().machine not in ("x86_64", "aarch64"):
        raise MapperError("This implementation requires 64-bit x86 or ARM Linux ioctl ABI")
    inspection = discover()
    if inspection["descriptor"]["sha256"] != DESCRIPTOR_SHA256:
        raise MapperError("RGR06 descriptor changed; use passive capture before mapping")
    evdev_nodes = [node for node in inspection["nodes"] if node["kind"] == "evdev"]
    raw_nodes = [node for node in inspection["nodes"] if node["kind"] == "hidraw"]
    if len(evdev_nodes) != 2 or len(raw_nodes) != 1:
        raise MapperError("Expected precisely two RGR06 evdev nodes and one hidraw node")
    stopped = False
    def stop(signum, frame):
        nonlocal stopped
        stopped = True
    # LIFO context exit keeps stop-only signal handling active through release,
    # device destruction, ungrab, and FD closure, including repeated signals.
    with CleanupLedger() as cleanup, ExitStack() as signal_stack, ExitStack() as stack:
        if not check:
            for signum in (signal.SIGINT, signal.SIGTERM):
                old = signal.signal(signum, stop)
                signal_stack.callback(signal.signal, signum, old)
        if not check:
            _lock(stack)
        opened = []
        for node in [*evdev_nodes, *raw_nodes]:
            fd, identity = open_verified(node, inspection)
            stack.callback(cleanup.run, os.close, fd)
            opened.append((node, fd, identity))
        raw_fd = opened[-1][1]
        uinput = _uinput_fd()
        os.close(uinput)  # Access/identity check only; no virtual device exists yet.
        if not check:
            print(f"Preflight: release all RGR06 controls, then briefly touch and release its surface within {arm_timeout:g} seconds.", flush=True)
        arm_deadline = time.monotonic() + arm_timeout
        first = _first_report(raw_fd, 0.5 if check else arm_timeout, lambda: stopped)
        status = {
            "device": inspection["identity"], "descriptor_sha256": DESCRIPTOR_SHA256,
            "opened_nodes": [node["path"] for node, _, _ in opened],
            "uinput_writable": True, "live_report_length": len(first) if first else None,
            "bindings": len(config.bindings), "touch_enabled": config.touch_enabled,
            "profile": config.profile, "controls": dict(config.controls),
            "touch_mode": config.touch_mode, "touch_gain": config.touch_gain,
            "grabbed": False, "virtual_devices_created": False,
        }
        print(json.dumps(status, ensure_ascii=False), flush=True)
        if check:
            return status
        if stopped:
            return {"stopped": "signal"}
        if first is None:
            raise MapperError("No live report observed during preflight; rerun and briefly touch RGR06 during the prompt")
        evdev_fds = [fd for _, fd, _ in opened[:2]]
        if not _wait_released(evdev_fds, arm_deadline, lambda: stopped):
            if stopped:
                return {"stopped": "signal"}
            raise MapperError("RGR06 controls did not become fully released within the preflight deadline")
        # No queued sample from before the grabs may be replayed into the desktop.
        for _, fd, _ in opened:
            _drain(fd)
        _require_released(evdev_fds)
        for _, fd, _ in opened[:2]:
            fcntl.ioctl(fd, EVIOCGRAB, 1)
            stack.callback(cleanup.run, fcntl.ioctl, fd, EVIOCGRAB, 0)
        _require_released(evdev_fds)
        _drain(raw_fd)
        if stopped:
            return {"stopped": "signal"}
        touch = VirtualInput("touch") if config.touch_enabled and config.touch_mode == "touchscreen" else None
        if touch:
            stack.callback(cleanup.run, touch.close)
        mouse = VirtualInput("mouse")
        stack.callback(cleanup.run, mouse.close)
        mouse_output = PacedMouse(mouse.frame, lambda: stopped)
        engine = MappingEngine(config, mouse_output.frame, touch.frame if touch else lambda events: None)
        stack.callback(cleanup.run, release_engine, engine, mouse_output)
        print(f"Mapping active for up to {seconds:g}s; Ctrl-C releases controls and restores native RGR06 input.", flush=True)
        deadline = time.monotonic() + seconds
        poller = select.poll()
        for _, fd, _ in opened:
            poller.register(fd, 0)
        while not stopped and time.monotonic() < deadline:
            if poller.poll(0):
                raise MapperError("RGR06 disconnected or an input node became unavailable")
            ready = select.select([raw_fd], [], [], min(0.25, max(0, deadline - time.monotonic())))[0]
            if ready:
                # Process queued reports before expiring any macro prefixes.
                # A fixed packet budget bounds continuous-device work per loop.
                drained = False
                for _ in range(256):
                    if stopped or time.monotonic() >= deadline:
                        break
                    try:
                        raw = os.read(raw_fd, 4096)
                    except BlockingIOError:
                        drained = True
                        break
                    if not raw:
                        raise MapperError("RGR06 disconnected")
                    try:
                        engine.feed(raw)
                    except MappingStopped:
                        stopped = True
                        break
                if drained:
                    engine.expire(time.monotonic())
            else:
                engine.expire(time.monotonic())
        if engine.firmware is not None:
            engine.firmware.finish()
        return {"stopped": "signal" if stopped else "duration_elapsed", "statistics": engine.statistics()}


def replay(config, path):
    """Replay preserved reports into counting sinks; never opens input devices."""
    counts, held, touch_active = Counter(), set(), False
    def mouse_frame(events):
        for event_type, code, value in events:
            if event_type == EV_KEY:
                if value and code not in held:
                    counts[f"button_down_{code}"] += 1
                    held.add(code)
                elif not value:
                    held.discard(code)
            elif event_type == EV_REL and code == REL_WHEEL:
                counts["wheel_up" if value > 0 else "wheel_down"] += abs(value)
            elif event_type == EV_REL and code in (REL_X, REL_Y):
                axis = "x" if code == REL_X else "y"
                counts[f"motion_{axis}_events"] += 1
                counts[f"motion_{axis}_total"] += value
    def touch_frame(events):
        nonlocal touch_active
        for event_type, code, value in events:
            if event_type == EV_KEY and code == BTN_TOUCH:
                if value and not touch_active:
                    counts["touch_contacts"] += 1
                elif not value and touch_active:
                    counts["touch_releases"] += 1
                touch_active = bool(value)
    engine = MappingEngine(config, mouse_frame, touch_frame)
    previous = None
    raw_reports = 0
    header_seen, complete = False, False
    raw_sources = set()
    digest = hashlib.sha256()
    with path.open("rb") as source:
        try:
            while True:
                line = source.readline(262145)
                if not line:
                    break
                if len(line) > 262144:
                    raise MapperError("Replay record exceeds 256 KiB")
                digest.update(line)
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise MapperError("Replay JSONL records must be objects")
                if record.get("kind") == "capture_start":
                    if header_seen or record.get("schema") != "rgr06-capture-v1":
                        raise MapperError("Replay requires one rgr06-capture-v1 capture_start header")
                    inspection = record.get("inspection")
                    if not isinstance(inspection, dict):
                        raise MapperError("Replay header is missing inspection provenance")
                    identity = inspection.get("identity")
                    expected = {"bus": TARGET_BUS, "vendor": TARGET_VENDOR,
                                "product": TARGET_PRODUCT, "uniq": TARGET_UNIQ}
                    if not isinstance(identity, dict) or any(identity.get(key) != value for key, value in expected.items()):
                        raise MapperError("Replay capture identity does not match the selected RGR06")
                    descriptor = inspection.get("descriptor")
                    if not isinstance(descriptor, dict) or descriptor.get("sha256") != DESCRIPTOR_SHA256:
                        raise MapperError("Replay capture descriptor does not match the pinned RGR06 descriptor")
                    nodes = inspection.get("nodes")
                    if not isinstance(nodes, list):
                        raise MapperError("Replay capture header is missing source nodes")
                    raw_sources = {node["path"] for node in nodes if isinstance(node, dict)
                                   and node.get("kind") == "hidraw" and isinstance(node.get("path"), str)}
                    if not raw_sources:
                        raise MapperError("Replay capture header does not identify a hidraw source")
                    header_seen = True
                if record.get("kind") == "capture_end":
                    complete = True
                if record.get("kind") != "hidraw_report":
                    continue
                if not header_seen or complete:
                    raise MapperError("Replay HID reports must follow capture_start and precede capture_end")
                if record.get("source") not in raw_sources:
                    raise MapperError("Replay HID report source is outside the inspected capture nodes")
                timestamp = record.get("received_monotonic_ns")
                if type(timestamp) is not int or not 0 <= timestamp <= (1 << 64) - 1 or (previous is not None and timestamp < previous):
                    raise MapperError("Replay timestamps must be monotonically ordered unsigned 64-bit nanoseconds")
                raw_hex = record.get("hex")
                if not isinstance(raw_hex, str):
                    raise MapperError("Replay HID reports require a hex string")
                previous = timestamp
                now = timestamp / 1_000_000_000
                engine.expire(now)
                engine.feed(bytes.fromhex(raw_hex), now)
                raw_reports += 1
            if not header_seen:
                raise MapperError("Replay is missing capture_start provenance")
            before_cleanup = {"buttons_held": sorted(held), "touch_active": touch_active}
        finally:
            engine.release_all()
    return {"source_path": str(path), "source_absolute_path": os.path.abspath(path),
            "source_sha256": digest.hexdigest(), "capture_complete": complete,
            "raw_reports": raw_reports, "state_before_final_cleanup": before_cleanup,
            "button_clicks": {name: counts[f"button_down_{code}"] for name, code in ACTIONS.items()},
            "wheel_detents": {name: counts[name] for name in WHEELS},
            "touch_contacts": counts["touch_contacts"], "touch_releases": counts["touch_releases"],
            "relative_motion": {axis: {"events": counts[f"motion_{axis}_events"],
                                      "total": counts[f"motion_{axis}_total"]} for axis in ("x", "y")},
            "buttons_held_at_end": sorted(held), "touch_active_at_end": touch_active,
            "statistics": engine.statistics()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="verify access and identity; no grabs or virtual devices")
    mode.add_argument("--run", action="store_true", help="temporarily grab only RGR06 and run the configured mapping")
    mode.add_argument("--replay", type=Path, help="replay captured JSONL into counters; no devices opened")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("rgr06-config.json"))
    parser.add_argument("--seconds", type=float, default=300, help="run duration, >0 and <=3600 (default: 300)")
    parser.add_argument("--arm-timeout", type=float, default=30, help="live-report preflight wait, >0 and <=120 (default: 30)")
    args = parser.parse_args(argv)
    if not math.isfinite(args.seconds) or not 0 < args.seconds <= 3600:
        parser.error("--seconds must be finite, >0, and <=3600")
    if not math.isfinite(args.arm_timeout) or not 0 < args.arm_timeout <= 120:
        parser.error("--arm-timeout must be finite, >0, and <=120")
    try:
        config = load_config(args.config)
        if args.replay:
            print(json.dumps(replay(config, args.replay), ensure_ascii=False))
        else:
            result = operate(config, check=args.check, seconds=args.seconds, arm_timeout=args.arm_timeout)
            if not args.check:
                print(json.dumps(result, ensure_ascii=False))
    except (CaptureError, MapperError, MotionError, OSError, ValueError, TypeError) as error:
        print(f"RGR06 mapper: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
