import math
import unittest

from rgr06_motion import MotionError, RelativeTouchMotion, validate_gain


def point(x, y=100, active=True):
    return {"kind": "digitizer", "in_range": active, "tip_switch": False, "x": x, "y": y}


class RelativeMotionTests(unittest.TestCase):
    def test_first_sample_only_baselines_and_lift_reposition_does_not_jump(self):
        motion = RelativeTouchMotion()
        self.assertEqual(motion.feed(point(1000)), [])
        self.assertEqual(motion.feed(point(1001)), [])
        self.assertEqual(motion.feed(point(1001, active=False)), [])
        self.assertEqual(motion.feed(point(3500)), [])
        self.assertEqual(motion.feed(point(3501)), [])
        self.assertEqual(motion.feed(point(3502)), [(2, 0, 1)])

    def test_positive_negative_carry_truncates_toward_zero_and_reverses(self):
        motion = RelativeTouchMotion(gain=0.5)
        expected = [[], [], [(2, 0, 1)], [], [(2, 0, -1)]]
        self.assertEqual([motion.feed(point(x)) for x in (100, 99, 102, 101, 100)], expected)
        negative = RelativeTouchMotion(gain=0.5)
        self.assertEqual([negative.feed(point(x)) for x in (100, 99, 98)], [[], [], [(2, 0, -1)]])

    def test_fractional_gain_does_not_accumulate_addition_drift(self):
        motion = RelativeTouchMotion(gain=0.1)
        events = [event for x in range(100, 111) for event in motion.feed(point(x))]
        self.assertEqual(events, [(2, 0, 1)])

    def test_axis_transforms_are_applied_once_before_differencing(self):
        motion = RelativeTouchMotion(gain=1, swap_xy=True, invert_x=True, invert_y=True)
        self.assertEqual(motion.feed(point(100, 200)), [])
        self.assertEqual(motion.feed(point(103, 205)), [(2, 0, -5), (2, 1, -3)])

    def test_saturation_does_not_invent_lost_motion_and_keeps_raw_axis_statistics(self):
        motion = RelativeTouchMotion(gain=1, swap_xy=True)
        self.assertEqual(motion.feed(point(4095, 100)), [])
        self.assertEqual(motion.feed(point(4095, 110)), [(2, 0, 10)])
        self.assertEqual(motion.feed(point(4095, 110)), [])
        self.assertEqual(motion.statistics()["axis_saturation_samples"], {"x": 3, "y": 0})

    def test_gain_and_int32_output_limits_are_expected_errors(self):
        for value in (0, -1, math.inf, math.nan, True, "0.5", 10**1000):
            with self.subTest(value=type(value)), self.assertRaises(MotionError):
                validate_gain(value)
        motion = RelativeTouchMotion(gain=1e308)
        motion.feed(point(1))
        with self.assertRaisesRegex(MotionError, "int32"):
            motion.feed(point(2))
        self.assertFalse(motion.statistics()["active"])

    def test_unknown_or_invalid_facts_reset_without_stale_motion(self):
        for invalid in (None, {}, point(4096), point(True), {**point(100), "in_range": None}):
            motion = RelativeTouchMotion()
            motion.feed(point(100))
            with self.subTest(invalid=invalid), self.assertRaises(MotionError):
                motion.feed(invalid)
            self.assertEqual(motion.feed(point(3000)), [])

    def test_reset_clears_carry_and_preserves_statistics(self):
        motion = RelativeTouchMotion()
        motion.feed(point(100))
        motion.feed(point(101))
        motion.reset()
        motion.reset()
        self.assertEqual(motion.feed(point(200)), [])
        self.assertEqual(motion.feed(point(201)), [])
        self.assertEqual(motion.statistics()["contacts"], 2)


if __name__ == "__main__":
    unittest.main()
