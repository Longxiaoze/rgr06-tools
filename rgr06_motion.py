"""Pure relative motion from verified RGR06 digitizer facts; no device access.

Callers must supply facts decoded under the matched, pinned HID descriptor.
InRange is the explicit default activity signal for this device's observed
motion reports. It is a mapping policy, not a claim of true screen contact.
Firmware coordinates can stop at 0/4095; motion lost there cannot be recovered.
"""
import math

EV_REL, REL_X, REL_Y = 2, 0, 1
INT32_MIN, INT32_MAX = -(1 << 31), (1 << 31) - 1
COORDINATE_MAX = 4095


class MotionError(ValueError):
    """Expected invalid facts, options, or unrepresentable motion."""


def validate_gain(value):
    if type(value) not in (int, float):
        raise MotionError("Pointer gain must be a finite positive number")
    try:
        normalized = float(value)
    except (OverflowError, ValueError) as error:
        raise MotionError("Pointer gain must be a finite positive number") from error
    if not math.isfinite(normalized) or normalized <= 0:
        raise MotionError("Pointer gain must be a finite positive number")
    return normalized


class RelativeTouchMotion:
    """Own activity, transformed baseline, exact fractional carry, and bounds.

    The first active sample only establishes a baseline. Lift/reset discards
    both axis carries, so repositioning a finger cannot cause a cursor jump.
    Integer ratios preserve the selected float gain without cumulative float
    addition drift; carries remain strictly smaller than one output unit.
    """

    def __init__(self, contact_signal="in_range", gain=0.5, swap_xy=False,
                 invert_x=False, invert_y=False):
        if contact_signal not in ("in_range", "tip_switch"):
            raise MotionError("contact_signal must explicitly name in_range or tip_switch")
        for name, option in (("swap_xy", swap_xy), ("invert_x", invert_x), ("invert_y", invert_y)):
            if type(option) is not bool:
                raise MotionError(f"{name} must be boolean")
        self.contact_signal, self.gain = contact_signal, validate_gain(gain)
        self.swap_xy, self.invert_x, self.invert_y = swap_xy, invert_x, invert_y
        self._numerator, self._denominator = self.gain.as_integer_ratio()
        self._samples = self._contacts = self._lifts = self._frames = self._rejected = 0
        self._saturated_x = self._saturated_y = self._total_x = self._total_y = 0
        self.reset()

    def reset(self):
        """Forget contact and fractional motion; retain cumulative statistics."""
        self._baseline = None
        self._carry_x = self._carry_y = 0

    def _facts(self, report):
        if not isinstance(report, dict) or report.get("kind") != "digitizer":
            raise MotionError("Expected decoded RGR06 digitizer facts")
        active = report.get(self.contact_signal)
        if type(active) is not bool:
            raise MotionError(f"Digitizer {self.contact_signal} must be an explicit boolean")
        x, y = report.get("x"), report.get("y")
        for name, value in (("x", x), ("y", y)):
            if type(value) is not int or not 0 <= value <= COORDINATE_MAX:
                raise MotionError(f"Digitizer {name} must be an integer in 0..{COORDINATE_MAX}")
        return active, x, y

    def _scaled(self, delta, carry):
        numerator = delta * self._numerator + carry
        # Python // rounds down; explicitly retain truncation toward zero.
        integer = abs(numerator) // self._denominator
        if numerator < 0:
            integer = -integer
        if not INT32_MIN <= integer <= INT32_MAX:
            raise MotionError("Scaled relative motion does not fit the Linux signed int32 input value")
        return integer, numerator - integer * self._denominator

    def feed(self, report):
        try:
            active, x, y = self._facts(report)
            if not active:
                if self._baseline is not None:
                    self._lifts += 1
                self.reset()
                return []
            self._samples += 1
            self._saturated_x += x in (0, COORDINATE_MAX)
            self._saturated_y += y in (0, COORDINATE_MAX)
            if self.swap_xy:
                x, y = y, x
            if self.invert_x:
                x = COORDINATE_MAX - x
            if self.invert_y:
                y = COORDINATE_MAX - y
            if self._baseline is None:
                self._contacts += 1
                self._baseline = (x, y)
                return []
            dx, carry_x = self._scaled(x - self._baseline[0], self._carry_x)
            dy, carry_y = self._scaled(y - self._baseline[1], self._carry_y)
            self._baseline, self._carry_x, self._carry_y = (x, y), carry_x, carry_y
            self._total_x += dx
            self._total_y += dy
            events = [(EV_REL, code, delta) for code, delta in ((REL_X, dx), (REL_Y, dy)) if delta]
            self._frames += bool(events)
            return events
        except MotionError:
            self._rejected += 1
            self.reset()  # A later valid sample must establish a fresh baseline.
            raise

    def statistics(self):
        return {"gain": self.gain, "contact_signal": self.contact_signal,
                "active_samples": self._samples, "contacts": self._contacts, "lifts": self._lifts,
                "motion_frames": self._frames, "relative_x_total": self._total_x,
                "relative_y_total": self._total_y, "rejected_reports": self._rejected,
                "active": self._baseline is not None,
                "axis_saturation_samples": {"x": self._saturated_x, "y": self._saturated_y},
                "limitations": ["Firmware coordinate saturation at 0/4095 loses motion; no missing movement is synthesized."]}
