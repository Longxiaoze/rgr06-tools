"""Run with /usr/bin/python3; constructs GDK objects without opening a GUI."""
from collections import Counter
import unittest
import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk
from rgr06_desktop_test import count_event, parse_event


class GdkBoundary(unittest.TestCase):
    def test_union_buttons_1_2_3_decode_to_integers_and_count(self):
        counts = Counter()
        for number in (1, 2, 3):
            for event_type, kind in ((Gdk.EventType.BUTTON_PRESS, "button_press"),
                                     (Gdk.EventType.BUTTON_RELEASE, "button_release")):
                event = Gdk.Event.new(event_type)
                event.button = number
                event.time, event.x, event.y = 321, 2.5, 4.5
                # The producing representation that caused the original bug.
                self.assertIsInstance(event.button, Gdk.EventButton)
                facts = parse_event(event, Gdk)
                self.assertEqual((facts["kind"], facts["button"], facts["gdk_time_ms"]), (kind, number, 321))
                self.assertEqual((facts["x"], facts["y"]), (2.5, 4.5))
                count_event(counts, facts)
                self.assertEqual(counts[f"{kind}_{number}"], 1)

    def test_touch_and_smooth_scroll_accessors_preserve_values(self):
        for event_type, kind in ((Gdk.EventType.TOUCH_BEGIN, "touch_begin"),
                                 (Gdk.EventType.TOUCH_UPDATE, "touch_update"),
                                 (Gdk.EventType.TOUCH_END, "touch_end")):
            event = Gdk.Event.new(event_type)
            event.x, event.y, event.time = 1.25, 3.5, 789
            facts = parse_event(event, Gdk)
            self.assertEqual((facts["kind"], facts["x"], facts["y"], facts["gdk_time_ms"]), (kind, 1.25, 3.5, 789))
        event = Gdk.Event.new(Gdk.EventType.SCROLL)
        event.direction, event.delta_x, event.delta_y = Gdk.ScrollDirection.SMOOTH, 0.5, -1.25
        facts = parse_event(event, Gdk)
        self.assertEqual((facts["direction"], facts["delta_x"], facts["delta_y"]), ("smooth", 0.5, -1.25))
        counts = Counter()
        count_event(counts, facts)
        self.assertEqual(counts["smooth_scroll_events"], 1)
        self.assertEqual(counts["smooth_scroll_delta_y"], -1.25)

    def test_touch_emulated_buttons_are_not_counted_as_mouse_clicks(self):
        facts = {"kind": "button_press", "button": 1, "pointer_emulated": True}
        counts = Counter()
        count_event(counts, facts)
        self.assertEqual(counts["pointer_emulated"], 1)
        self.assertEqual(counts["button_press_1"], 0)


if __name__ == "__main__":
    unittest.main()
