"""Regression checks use exact HID packets, without grabbing or injecting input."""

from contextlib import ExitStack
import errno
import json
import os
from pathlib import Path
import select
import struct
import tempfile
import unittest
from unittest.mock import Mock, patch

import rgr06_mapper as mapper


def keyboard(*usages, modifiers=0):
    return bytes((1, modifiers, 0, *usages, *([0] * (6 - len(usages)))))


def consumer(usage):
    return b"\x02" + struct.pack("<H", usage)


def touch(flags, x=0, y=0, count=0):
    return bytes((3, flags, count)) + struct.pack("<HHHH", x, y, 0, 0)


def config(bindings=(), **touch_options):
    touch_options.setdefault("mode", "touchscreen")
    return mapper.parse_config({
        "schema_version": 1, "device": {"uniq": mapper.TARGET_UNIQ},
        "bindings": list(bindings), "touch": touch_options,
    })


def observed_config():
    return mapper.parse_config({
        "schema_version": 1, "profile": "rgr06-observed-v1",
        "device": {"uniq": mapper.TARGET_UNIQ},
        "touch": {"contact_signal": "in_range", "mode": "touchscreen"},
        "controls": {"left": "left", "right": "right", "wheel_press": "middle",
                     "wheel_forward": "wheel_up", "wheel_backward": "wheel_down"},
    })


def binding(report_id, usage, action, modifiers=None):
    source = {"report_id": report_id, "usage": usage}
    if modifiers is not None:
        source["modifiers"] = modifiers
    return {"source": source, "action": action}


class InputTransitions(unittest.TestCase):
    def engine(self, settings):
        mouse, screen = [], []
        return mapper.MappingEngine(settings, mouse.append, screen.append), mouse, screen

    def test_shared_button_owners_and_unrelated_report_release(self):
        settings = config((binding(1, 4, "left"), binding(2, 233, "left")))
        engine, mouse, _ = self.engine(settings)
        engine.feed(keyboard(4))
        engine.feed(consumer(233))
        engine.feed(keyboard())
        self.assertEqual(mouse, [[(mapper.EV_KEY, mapper.BTN_LEFT, 1)]])
        engine.feed(consumer(0))
        self.assertEqual(mouse[-1], [(mapper.EV_KEY, mapper.BTN_LEFT, 0)])
        self.assertEqual(engine.buttons_down, set())

    def test_wheel_edges_ignore_duplicate_reports_and_do_not_release_held_mouse(self):
        settings = config((binding(1, 4, "middle"), binding(2, 233, "wheel_up")))
        engine, mouse, _ = self.engine(settings)
        engine.feed(keyboard(4))
        for usage in (233, 233, 0, 233, 0):
            engine.feed(consumer(usage))
        self.assertEqual(mouse, [[(1, mapper.BTN_MIDDLE, 1)], [(2, 8, 1)], [(2, 8, 1)]])
        engine.release_all()
        self.assertEqual(mouse[-1], [(1, mapper.BTN_MIDDLE, 0)])

    def test_exact_modifiers_and_independent_release(self):
        engine, mouse, _ = self.engine(config((binding(1, 6, "right", 1),)))
        engine.feed(keyboard(6))
        self.assertEqual(mouse, [])
        engine.feed(keyboard(6, modifiers=1))
        engine.feed(keyboard(6, modifiers=3))
        self.assertEqual(mouse, [[(1, mapper.BTN_RIGHT, 1)], [(1, mapper.BTN_RIGHT, 0)]])

    def test_actual_in_range_report_is_not_default_contact(self):
        engine, _, screen = self.engine(config())
        engine.feed(touch(2, 3401, 156))
        engine.feed(touch(0))
        self.assertEqual(screen, [])

    def test_explicit_in_range_adapter_and_touch_frame_order(self):
        engine, mouse, screen = self.engine(config(contact_signal="in_range", invert_x=True))
        engine.feed(touch(2, 3401, 156))
        engine.feed(touch(2, 3800, 4095))
        engine.feed(touch(0, 65535, 65535))
        self.assertEqual(screen[0], [(1, mapper.BTN_TOUCH, 1), (3, 0, 694), (3, 1, 156)])
        self.assertEqual(screen[-1], [(1, mapper.BTN_TOUCH, 0)])
        self.assertFalse(engine.touch_down)
        self.assertEqual(mouse, [])

    def test_bad_packet_never_emits_but_finally_releases_previous_state(self):
        for bad in (b"\x01", b"\x04\x00", keyboard(1), touch(3, 5000, 100), touch(3, count=2)):
            with self.subTest(bad=bad.hex()):
                engine, mouse, screen = self.engine(config((binding(1, 4, "left"),)))
                engine.feed(keyboard(4))
                engine.feed(touch(3, 120, 230))
                before = (len(mouse), len(screen))
                try:
                    with self.assertRaises(mapper.MapperError):
                        engine.feed(bad)
                    self.assertEqual((len(mouse), len(screen)), before)
                finally:
                    engine.release_all()
                self.assertEqual(mouse[-1], [(1, mapper.BTN_LEFT, 0)])
                self.assertEqual(screen[-1], [(1, mapper.BTN_TOUCH, 0)])

    def test_failed_mouse_release_still_releases_touch_and_can_retry(self):
        engine, mouse, screen = self.engine(config((binding(1, 4, "left"),)))
        engine.feed(keyboard(4))
        engine.feed(touch(3, 120, 230))
        writer = engine.mouse_frame
        engine.mouse_frame = Mock(side_effect=mapper.MapperError("write timeout"))
        with self.assertRaisesRegex(mapper.MapperError, "write timeout"):
            engine.release_all()
        self.assertTrue(engine.buttons_down)
        self.assertFalse(engine.touch_down)
        self.assertEqual(screen[-1], [(1, mapper.BTN_TOUCH, 0)])
        engine.mouse_frame = writer
        engine.release_all()
        self.assertEqual(mouse[-1], [(1, mapper.BTN_LEFT, 0)])

    def test_partial_button_write_retains_possible_down_for_cleanup(self):
        engine, _, _ = self.engine(config((binding(1, 4, "left"),)))
        engine.mouse_frame = Mock(side_effect=OSError(errno.EIO, "failed"))
        with self.assertRaises(OSError):
            engine.feed(keyboard(4))
        released = []
        engine.mouse_frame = released.append
        engine.release_all()
        self.assertEqual(released, [[(1, mapper.BTN_LEFT, 0)]])


class ConfigBoundaries(unittest.TestCase):
    def test_bad_identity_types_are_expected_errors(self):
        for bad in (None, 5, [], {}, True):
            with self.subTest(bad=bad):
                value = {"schema_version": 1, "device": {"uniq": bad}, "bindings": []}
                with self.assertRaises(mapper.MapperError):
                    mapper.parse_config(value)

    def test_duplicate_sources_and_invalid_action_fail_before_devices(self):
        one = binding(1, 4, "left")
        with self.assertRaises(mapper.MapperError):
            config((one, one))
        for bad in ([], None, 3, "click"):
            with self.subTest(action=bad), self.assertRaises(mapper.MapperError):
                config((binding(1, 4, bad),))


class FirmwareRecognition(unittest.TestCase):
    def setUp(self):
        self.mouse, self.screen = [], []
        self.engine = mapper.MappingEngine(observed_config(), self.mouse.append, self.screen.append)

    def tap(self, usage, now, report_id=1):
        packet = consumer(usage) if report_id == 2 else keyboard(usage)
        neutral = consumer(0) if report_id == 2 else keyboard()
        self.engine.expire(now)
        self.engine.feed(packet, now)
        self.engine.feed(neutral, now + 0.001)

    def test_f1_prefix_alone_and_wheel_do_not_generate_a_right_click(self):
        self.tap(0x3A, 0)
        self.assertEqual(self.mouse, [])
        self.tap(0x50, 0.002)
        self.assertEqual(self.mouse, [[(mapper.EV_REL, mapper.REL_WHEEL, 1)]])
        self.tap(0x52, 0.204)
        self.assertEqual(self.mouse, [[(mapper.EV_REL, mapper.REL_WHEEL, 1)]])
        self.assertEqual(self.engine.firmware.statistics()["recognized_controls"], {"wheel_forward": 1})

    def test_delayed_single_right_and_middle_are_full_click_pulses(self):
        self.tap(0x3A, 0)
        self.tap(0x28, 0.383)
        self.tap(0x3A, 2)
        self.tap(0x3D, 2.374)
        self.assertEqual(self.mouse, [[(1, mapper.BTN_RIGHT, 1)], [(1, mapper.BTN_RIGHT, 0)],
                                     [(1, mapper.BTN_MIDDLE, 1)], [(1, mapper.BTN_MIDDLE, 0)]])
        self.assertEqual(self.engine.buttons_down, set())

    def test_captured_double_click_suffixes_emit_two_clicks_each(self):
        self.tap(0x3A, 0)
        self.tap(0x3A, 0.279)
        self.tap(0x224, 0.646, report_id=2)
        self.tap(0x3A, 2)
        self.tap(0x3A, 2.351)
        self.tap(0x3E, 2.697)
        downs = [events[0][1] for events in self.mouse if events[0][2] == 1]
        self.assertEqual(downs, [mapper.BTN_RIGHT, mapper.BTN_RIGHT, mapper.BTN_MIDDLE, mapper.BTN_MIDDLE])

    def test_expired_incomplete_and_unknown_sequences_drop_without_clicks(self):
        self.tap(0x3A, 0)
        self.engine.expire(1.01)
        self.tap(0x28, 1.02)
        self.tap(0x3A, 2)
        self.tap(0x60, 2.1)
        self.tap(0x3D, 2.2)
        self.engine.release_all()
        self.assertEqual(self.mouse, [])
        stats = self.engine.firmware.statistics()
        self.assertEqual(stats["pending_taps"], 0)
        self.assertEqual(stats["drops"]["incomplete_sequences"], 1)
        self.assertGreater(stats["drops"]["unknown_sequences"], 0)

    def test_non_neutral_key_change_cannot_fabricate_a_valid_tap(self):
        self.engine.feed(keyboard(0x3A), 0)
        self.engine.feed(keyboard(0x3C), 0.001)
        self.engine.feed(keyboard(), 0.002)
        self.assertEqual(self.mouse, [])
        self.assertEqual(self.engine.firmware.statistics()["drops"]["malformed_transitions"], 1)

    def test_overlapping_report_states_are_discarded_until_neutral(self):
        self.engine.feed(keyboard(0x3A), 0)
        self.engine.feed(consumer(0x224), 0.001)
        self.engine.feed(keyboard(), 0.002)
        self.engine.feed(consumer(0), 0.003)
        self.assertEqual(self.mouse, [])
        self.assertEqual(self.engine.firmware.statistics()["drops"]["overlapping_report_states"], 1)

    def test_unknown_chord_stays_quarantined_after_timeout_until_neutral(self):
        self.engine.feed(keyboard(0x3A, 0x3C), 0)
        self.engine.expire(2)
        self.engine.feed(keyboard(0x3C), 2.001)
        self.engine.feed(keyboard(), 2.002)
        self.assertEqual(self.mouse, [])
        self.tap(0x3C, 2.1)
        self.assertEqual(self.mouse, [[(1, mapper.BTN_LEFT, 1)], [(1, mapper.BTN_LEFT, 0)]])

    def test_expired_held_tap_cannot_restart_from_repeated_down(self):
        self.engine.feed(keyboard(0x3C), 0)
        self.engine.expire(2)
        self.engine.expire(4)
        self.engine.feed(keyboard(0x3C), 4.001)
        self.engine.feed(keyboard(), 4.002)
        self.engine.release_all()
        self.assertEqual(self.mouse, [])
        self.assertEqual(self.engine.firmware.statistics()["drops"]["incomplete_taps"], 1)

    def test_wheel_burst_repeats_and_reverses_without_extra_tail_unit(self):
        for index, usage in enumerate((0x3A, 0x4F, 0x4F, 0x50, 0x50, 0x50, 0x51)):
            self.tap(usage, index * 0.05)
        self.assertEqual(self.mouse, [[(2, mapper.REL_WHEEL, value)] for value in (-1, -1, 1, 1, 1)])
        self.assertFalse(self.engine.firmware.wheel_active)
        self.assertEqual(self.engine.firmware.statistics()["recognized_controls"],
                         {"wheel_backward": 2, "wheel_forward": 3})

    def test_unprefixed_or_expired_direction_pulses_do_not_create_a_burst(self):
        self.tap(0x50, 0)
        self.tap(0x52, 0.2)
        self.assertEqual(self.mouse, [])
        self.tap(0x3A, 2)
        self.tap(0x4F, 2.001)
        self.engine.expire(3.1)
        self.tap(0x4F, 3.11)
        self.tap(0x51, 3.31)
        self.assertEqual(self.mouse, [[(2, mapper.REL_WHEEL, -1)]])
        self.assertEqual(self.engine.firmware.statistics()["drops"]["incomplete_wheel_bursts"], 1)

    def test_complete_retest_capture_preserves_all_direction_pulses(self):
        path = Path(__file__).with_name("evidence") / "retest-samples.jsonl"
        if not path.exists():
            self.skipTest("Local hardware retest capture is not available")
        result = mapper.replay(observed_config(), path)
        self.assertTrue(result["capture_complete"])
        self.assertEqual(result["raw_reports"], 1468)
        self.assertEqual(result["wheel_detents"], {"wheel_up": 49, "wheel_down": 51})
        self.assertEqual(result["button_clicks"], {"left": 8, "right": 136, "middle": 11})
        self.assertFalse(result["statistics"]["wheel_burst_active"])

    def test_two_profiles_cannot_inject_in_parallel(self):
        value = {"schema_version": 1, "profile": "rgr06-observed-v1",
                 "device": {"uniq": mapper.TARGET_UNIQ}, "bindings": [binding(1, 0x3A, "right")]}
        with self.assertRaisesRegex(mapper.MapperError, "cannot be active together"):
            mapper.parse_config(value)

    def test_actual_125_report_capture_matches_declared_control_counts(self):
        path = Path(__file__).with_name("evidence") / "control-samples.jsonl"
        if not path.exists():
            self.skipTest("Local hardware capture is not available")
        result = mapper.replay(observed_config(), path)
        self.assertEqual(result["raw_reports"], 125)
        self.assertEqual(result["button_clicks"], {"left": 3, "right": 3, "middle": 3})
        self.assertEqual(result["wheel_detents"], {"wheel_up": 3, "wheel_down": 3})
        self.assertEqual((result["touch_contacts"], result["touch_releases"]), (1, 1))
        self.assertFalse(result["touch_active_at_end"])
        self.assertEqual(result["buttons_held_at_end"], [])
        self.assertFalse(any(result["statistics"]["drops"].values()))


class ReplayBoundaries(unittest.TestCase):
    def header(self):
        return {"kind": "capture_start", "schema": "rgr06-capture-v1", "inspection": {
            "identity": {"bus": mapper.TARGET_BUS, "vendor": mapper.TARGET_VENDOR,
                         "product": mapper.TARGET_PRODUCT, "uniq": mapper.TARGET_UNIQ},
            "descriptor": {"sha256": mapper.DESCRIPTOR_SHA256},
            "nodes": [{"kind": "hidraw", "path": "/selected/raw"}]}}

    def check_bad_records(self, records):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.jsonl"
            path.write_text("\n".join(json.dumps(record) for record in records))
            with self.assertRaises(mapper.MapperError):
                mapper.replay(observed_config(), path)

    def test_missing_or_foreign_provenance_is_rejected(self):
        packet = {"kind": "hidraw_report", "source": "/selected/raw", "hex": keyboard(0x3C).hex(),
                  "received_monotonic_ns": 10}
        self.check_bad_records([packet])
        wrong = self.header()
        wrong["inspection"]["descriptor"]["sha256"] = "wrong"
        self.check_bad_records([wrong, packet])
        wrong = self.header()
        wrong["inspection"]["identity"]["uniq"] = "other-device"
        self.check_bad_records([wrong, packet])

    def test_nonobjects_and_missing_packet_fields_are_expected_errors(self):
        self.check_bad_records([[]])
        self.check_bad_records([self.header(), {"kind": "hidraw_report", "source": "/selected/raw",
                                                "received_monotonic_ns": 10}])


class RealMouseTiming(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.frames, self.waits = [], []
        self.stopped = False

    def clock(self):
        return self.now

    def wait(self, seconds):
        self.waits.append(seconds)
        self.now += seconds

    def emit(self, events):
        self.frames.append((self.now, events))

    def output(self, emit=None, wait=None):
        return mapper.PacedMouse(emit or self.emit, lambda: self.stopped,
                                 clock=self.clock, wait=wait or self.wait)

    def test_real_double_click_has_four_separated_edges_first_immediate(self):
        output = self.output()
        engine = mapper.MappingEngine(observed_config(), output.frame, lambda events: None)
        for usage in (0x3A, 0x3A):
            engine.feed(keyboard(usage), 0)
            engine.feed(keyboard(), 0)
        engine.feed(consumer(0x224), 0)
        engine.feed(consumer(0), 0)
        self.assertEqual([events for _, events in self.frames],
                         [[(1, mapper.BTN_RIGHT, value)] for value in (1, 0, 1, 0)])
        times = [timestamp for timestamp, _ in self.frames]
        self.assertEqual(times[0], 0)
        for before, after in zip(times, times[1:]):
            self.assertGreaterEqual(after - before + 1e-12, 0.030)
        self.assertEqual(engine.buttons_down, set())

    def test_stop_during_wait_keeps_pressed_ledger_and_cleanup_releases_without_wait(self):
        def stop_wait(seconds):
            self.wait(seconds)
            self.stopped = True
        output = self.output(wait=stop_wait)
        engine = mapper.MappingEngine(observed_config(), output.frame, lambda events: None)
        with self.assertRaises(mapper.MappingStopped):
            engine.feed(keyboard(0x3C), 0)
            engine.feed(keyboard(), 0)
        self.assertEqual(engine.buttons_down, {mapper.BTN_LEFT})
        waits_before = len(self.waits)
        time_before = self.now
        mapper.release_engine(engine, output)
        self.assertEqual(len(self.waits), waits_before)
        self.assertEqual(self.now, time_before)
        self.assertEqual(self.frames[-1], (time_before, [(1, mapper.BTN_LEFT, 0)]))
        self.assertEqual(engine.buttons_down, set())

    def test_failed_press_write_is_released_without_cleanup_delay(self):
        def fail_once(events):
            if not self.frames:
                self.frames.append((self.now, events))
                raise OSError(errno.EIO, "partial write")
            self.emit(events)
        output = self.output(emit=fail_once)
        engine = mapper.MappingEngine(observed_config(), output.frame, lambda events: None)
        with self.assertRaises(OSError):
            engine.feed(keyboard(0x3C), 0)
            engine.feed(keyboard(), 0)
        mapper.release_engine(engine, output)
        self.assertEqual(self.waits, [])
        self.assertEqual(self.frames[-1], (0, [(1, mapper.BTN_LEFT, 0)]))
        self.assertEqual(engine.buttons_down, set())


class PointerTouchIntegration(unittest.TestCase):
    def test_relative_motion_has_no_first_contact_jump_click_or_screen_contact(self):
        mouse, screen = [], []
        engine = mapper.MappingEngine(config(mode="pointer", contact_signal="in_range", gain=0.5),
                                      mouse.append, screen.append)
        engine.feed(touch(2, 1000, 2000))
        self.assertEqual(mouse, [])
        engine.feed(touch(2, 1020, 1990))
        self.assertEqual(mouse, [[(2, mapper.REL_X, 10), (2, mapper.REL_Y, -5)]])
        engine.feed(touch(0, 1020, 1990))
        engine.feed(touch(2, 3000, 3000))
        self.assertEqual(len(mouse), 1)
        engine.release_all()
        self.assertFalse(engine.touch_down)
        engine.feed(touch(2, 10, 20))
        self.assertEqual(len(mouse), 1)
        self.assertEqual(screen, [])


class Lifecycle(unittest.TestCase):
    def boundary(self, stack, *, grab_failure=False, runtime_packets=(), cleanup_failure=False,
                 signal_during_cleanup=False):
        log = []
        handlers = {}
        nodes = [{"kind": "evdev", "path": "/selected/a"},
                 {"kind": "evdev", "path": "/selected/b"},
                 {"kind": "hidraw", "path": "/selected/raw"}]
        inspection = {"identity": {"uniq": mapper.TARGET_UNIQ}, "nodes": nodes,
                      "descriptor": {"sha256": mapper.DESCRIPTOR_SHA256}}
        def verified(node, inspection):
            fd = {"/selected/a": 10, "/selected/b": 11, "/selected/raw": 12}[node["path"]]
            log.append(("open", fd))
            return fd, {}
        def ioctl(fd, request, argument):
            log.append(("grab", fd, argument))
            if grab_failure and fd == 11 and argument == 1:
                raise OSError(errno.EBUSY, "already grabbed")
        class Virtual:
            def __init__(self, kind):
                self.kind = kind
                log.append(("create", kind))
            def frame(self, events):
                log.append(("frame", self.kind, events))
            def close(self):
                log.append(("destroy", self.kind))
                if signal_during_cleanup and self.kind == "mouse":
                    for _ in range(2):
                        handlers[mapper.signal.SIGINT](mapper.signal.SIGINT, None)
                    log.append(("signals_during_cleanup",))
                if cleanup_failure and self.kind == "mouse":
                    raise OSError(errno.EIO, "destroy failed")
        def close(fd):
            log.append(("close", fd))
        def signal_change(number, handler):
            log.append(("signal", number, handler))
            handlers[number] = handler
            return signal_change
        for name, replacement in {
            "discover": Mock(return_value=inspection), "open_verified": verified,
            "_lock": lambda stack: None, "_uinput_fd": lambda: 99,
            "_first_report": lambda *args: keyboard(), "_drain": lambda fd: None,
            "_require_released": lambda fds: None, "_wait_released": lambda *args: True,
            "VirtualInput": Virtual,
        }.items():
            stack.enter_context(patch.object(mapper, name, replacement))
        stack.enter_context(patch.object(mapper.os, "close", close))
        stack.enter_context(patch.object(mapper.fcntl, "ioctl", ioctl))
        stack.enter_context(patch.object(mapper.signal, "signal", signal_change))
        poller = Mock()
        poller.poll.side_effect = [[] for _ in runtime_packets] + [[(12, select.POLLHUP)]]
        stack.enter_context(patch.object(mapper.select, "poll", return_value=poller))
        stack.enter_context(patch.object(mapper.select, "select", return_value=([12], [], [])))
        reads = []
        for packet in runtime_packets:
            reads.extend((packet, BlockingIOError()))
        stack.enter_context(patch.object(mapper.os, "read", side_effect=reads))
        return log

    def test_second_grab_failure_rolls_back_first_without_virtual_creation(self):
        with ExitStack() as stack:
            log = self.boundary(stack, grab_failure=True)
            with self.assertRaises(OSError):
                mapper.operate(config(), check=False, seconds=60)
        self.assertEqual([x for x in log if x[0] == "grab"], [("grab", 10, 1), ("grab", 11, 1), ("grab", 10, 0)])
        self.assertFalse(any(x[0] == "create" for x in log))
        self.assertTrue(all(("close", fd) in log for fd in (10, 11, 12)))
        first_grab = next(i for i, x in enumerate(log) if x[0] == "grab")
        self.assertTrue(all(log.index(("open", fd)) < first_grab for fd in (10, 11, 12)))

    def test_disconnect_releases_both_devices_before_destroy_ungrab_and_signal_restore(self):
        with ExitStack() as stack:
            log = self.boundary(stack, runtime_packets=(keyboard(4), touch(3, 100, 200)))
            with self.assertRaisesRegex(mapper.MapperError, "disconnected"):
                mapper.operate(config((binding(1, 4, "left"),)), check=False, seconds=60)
        release_mouse = log.index(("frame", "mouse", [(1, mapper.BTN_LEFT, 0)]))
        release_touch = log.index(("frame", "touch", [(1, mapper.BTN_TOUCH, 0)]))
        destroy = next(i for i, x in enumerate(log) if x[0] == "destroy")
        ungrab = next(i for i, x in enumerate(log) if x[0] == "grab" and x[-1] == 0)
        self.assertLess(max(release_mouse, release_touch), destroy)
        self.assertLess(destroy, ungrab)
        self.assertTrue(all(log.index(("close", fd)) > ungrab for fd in (10, 11, 12)))
        self.assertEqual([x[0] for x in log[-2:]], ["signal", "signal"])

    def test_cleanup_failure_is_error_after_other_cleanup_completes(self):
        with ExitStack() as stack:
            log = self.boundary(stack, cleanup_failure=True)
            with self.assertRaisesRegex(mapper.MapperError, "Cleanup failed"):
                mapper.operate(config(), check=False, seconds=0)
        self.assertIn(("destroy", "touch"), log)
        self.assertIn(("grab", 10, 0), log)
        self.assertIn(("grab", 11, 0), log)
        self.assertTrue(all(("close", fd) in log for fd in (10, 11, 12)))

    def test_repeated_cancellation_during_final_cleanup_does_not_skip_ungrab_or_close(self):
        with ExitStack() as stack:
            log = self.boundary(stack, signal_during_cleanup=True)
            mapper.operate(config(), check=False, seconds=0)
        self.assertIn(("signals_during_cleanup",), log)
        self.assertIn(("destroy", "touch"), log)
        self.assertIn(("grab", 10, 0), log)
        self.assertIn(("grab", 11, 0), log)
        self.assertTrue(all(("close", fd) in log for fd in (10, 11, 12)))
        self.assertEqual([x[0] for x in log[-2:]], ["signal", "signal"])

    def test_first_touch_can_finish_releasing_before_grab(self):
        with patch.object(mapper.time, "monotonic", return_value=0), \
             patch.object(mapper, "_keys_released", side_effect=(False, False, True)) as released, \
             patch.object(mapper.select, "select") as wait:
            self.assertTrue(mapper._wait_released([10, 11], 30, lambda: False))
        self.assertEqual(released.call_count, 3)
        self.assertEqual(wait.call_count, 2)

    def test_check_never_grabs_or_creates_virtual_devices(self):
        with ExitStack() as stack:
            log = self.boundary(stack)
            mapper.operate(config(), check=True, seconds=1)
        self.assertFalse(any(x[0] in ("grab", "create", "frame") for x in log))

    def test_pointer_mode_creates_only_mouse_and_disconnect_has_no_touch_contact(self):
        with ExitStack() as stack:
            log = self.boundary(stack, runtime_packets=(touch(2, 100, 200), touch(2, 120, 190)))
            with self.assertRaisesRegex(mapper.MapperError, "disconnected"):
                mapper.operate(config(mode="pointer", contact_signal="in_range"), check=False, seconds=60)
        self.assertEqual([entry for entry in log if entry[0] == "create"], [("create", "mouse")])
        frames = [entry for entry in log if entry[0] == "frame"]
        self.assertEqual(frames, [("frame", "mouse", [(2, mapper.REL_X, 10), (2, mapper.REL_Y, -5)])])
        self.assertTrue(all(("close", fd) in log for fd in (10, 11, 12)))

    def test_uinput_open_failure_after_fd_acquisition_closes_fd(self):
        with patch.object(mapper.Path, "read_text", return_value="10:223"), \
             patch.object(mapper.os, "open", return_value=90), \
             patch.object(mapper.os, "fstat", side_effect=OSError(errno.EIO, "failed")), \
             patch.object(mapper.os, "close") as close:
            with self.assertRaises(OSError):
                mapper._uinput_fd()
        close.assert_called_once_with(90)


if __name__ == "__main__":
    unittest.main()
