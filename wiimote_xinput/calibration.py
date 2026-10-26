"""User calibration: data model, overrides, and guided calibration procedures.

Every Wiimote ships with factory calibration (read from its EEPROM and from
the extension). Factory values drift with age and are often wrong on
third-party Nunchuks, so the user can replace any part of them. A user
calibration is stored per Wiimote (keyed by its HID serial) and applied on
top of the factory values the moment it is saved.

The ``*Calibrator`` classes are UI-independent state machines: the UI feeds
them :class:`RawSnapshot` samples and shows their instructions and errors.
"""

from __future__ import annotations

import copy
import math
import statistics
from dataclasses import dataclass, field, replace
from typing import Any, Sequence

from .parser import (
    IR_IDENTITY,
    AccelCalibration,
    AxisCalibration,
    ClassicCalibration,
    ClassicRaw,
    IrCalibration,
    IrDot,
    NunchukCalibration,
    NunchukRaw,
    WiimoteState,
)

Vec2 = tuple[float, float]
Axis = tuple[int, int, int]  # (minimum, center, maximum)


# --------------------------------------------------------------------------
# Data model
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DeviceCalibration:
    """User overrides for one Wiimote. ``None`` fields use factory values."""

    accel: AccelCalibration | None = None
    nunchuk_accel: AccelCalibration | None = None
    nunchuk_stick: tuple[Axis, Axis] | None = None
    classic_left: tuple[Axis, Axis] | None = None
    classic_right: tuple[Axis, Axis] | None = None
    classic_triggers: tuple[tuple[int, int], tuple[int, int]] | None = None
    ir: IrCalibration | None = None

    def is_empty(self) -> bool:
        return all(getattr(self, f) is None for f in self.__dataclass_fields__)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in ("accel", "nunchuk_accel"):
            cal = getattr(self, name)
            if cal is not None:
                out[name] = {"zero": list(cal.zero), "one_g": list(cal.one_g)}
        for name in ("nunchuk_stick", "classic_left", "classic_right"):
            axes = getattr(self, name)
            if axes is not None:
                out[name] = {"x": list(axes[0]), "y": list(axes[1])}
        if self.classic_triggers is not None:
            out["classic_triggers"] = {"lt": list(self.classic_triggers[0]), "rt": list(self.classic_triggers[1])}
        if self.ir is not None:
            out["ir"] = {"center": list(self.ir.center), "half_range": list(self.ir.half_range)}
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "DeviceCalibration":
        """Parse stored calibration, silently dropping anything invalid."""
        d = d if isinstance(d, dict) else {}
        kwargs: dict[str, Any] = {}
        for name in ("accel", "nunchuk_accel"):
            v = d.get(name)
            try:
                zero = _int_tuple(v["zero"], 3, 0, 1023)
                one = _int_tuple(v["one_g"], 3, 0, 1023)
                if all(o - z >= 20 for z, o in zip(zero, one)):
                    kwargs[name] = AccelCalibration(zero=zero, one_g=one)
            except (TypeError, KeyError, ValueError):
                pass
        for name in ("nunchuk_stick", "classic_left", "classic_right"):
            v = d.get(name)
            try:
                axes = (_int_tuple(v["x"], 3, 0, 255), _int_tuple(v["y"], 3, 0, 255))
                if all(_axis_ok(a) for a in axes):
                    kwargs[name] = axes
            except (TypeError, KeyError, ValueError):
                pass
        v = d.get("classic_triggers")
        try:
            lt, rt = _int_tuple(v["lt"], 2, 0, 31), _int_tuple(v["rt"], 2, 0, 31)
            if lt[1] > lt[0] and rt[1] > rt[0]:
                kwargs["classic_triggers"] = (lt, rt)
        except (TypeError, KeyError, ValueError):
            pass
        v = d.get("ir")
        try:
            c = tuple(float(x) for x in v["center"])
            hr = tuple(float(x) for x in v["half_range"])
            if len(c) == 2 and len(hr) == 2 and all(abs(x) <= 1.5 for x in c) and all(0.02 <= abs(x) <= 2 for x in hr):
                kwargs["ir"] = IrCalibration(center=c, half_range=hr)  # type: ignore[arg-type]
        except (TypeError, KeyError, ValueError):
            pass
        return cls(**kwargs)


def _int_tuple(values: Any, n: int, lo: int, hi: int) -> tuple[int, ...]:
    t = tuple(int(x) for x in values)
    if len(t) != n or any(not lo <= x <= hi for x in t):
        raise ValueError
    return t


def _axis_ok(a: Axis) -> bool:
    return a[0] < a[1] < a[2]


EMPTY_CALIBRATION = DeviceCalibration()


@dataclass(frozen=True)
class EffectiveCalibration:
    accel: AccelCalibration
    nunchuk: NunchukCalibration
    classic: ClassicCalibration
    ir: IrCalibration


def apply_overrides(
    accel: AccelCalibration,
    nunchuk: NunchukCalibration,
    classic: ClassicCalibration,
    user: DeviceCalibration | None,
) -> EffectiveCalibration:
    """Factory calibration + user overrides -> calibration objects to use.

    Returns fresh copies, so the factory objects are never modified. User-set
    stick ranges are authoritative, so range learning is disabled for them.
    """
    user = user or EMPTY_CALIBRATION
    nun = copy.deepcopy(nunchuk)
    cls = copy.deepcopy(classic)
    if user.nunchuk_accel is not None:
        nun.accel = user.nunchuk_accel
    if user.nunchuk_stick is not None:
        nun.stick_x = AxisCalibration(*user.nunchuk_stick[0], learn=False)
        nun.stick_y = AxisCalibration(*user.nunchuk_stick[1], learn=False)
    if user.classic_left is not None:
        cls.lx = AxisCalibration(*user.classic_left[0], learn=False)
        cls.ly = AxisCalibration(*user.classic_left[1], learn=False)
    if user.classic_right is not None:
        cls.rx = AxisCalibration(*user.classic_right[0], learn=False)
        cls.ry = AxisCalibration(*user.classic_right[1], learn=False)
    if user.classic_triggers is not None:
        cls.lt_range, cls.rt_range = user.classic_triggers
    return EffectiveCalibration(
        accel=user.accel or accel,
        nunchuk=nun,
        classic=cls,
        ir=user.ir or IR_IDENTITY,
    )


@dataclass(frozen=True)
class RawSnapshot:
    """Uncalibrated sensor values (plus the calibrated state) for one report."""

    accel: tuple[int, int, int] | None = None
    nunchuk: NunchukRaw | None = None
    classic: ClassicRaw | None = None
    ir_dots: tuple[IrDot, ...] = ()
    pointer_raw: Vec2 | None = None  # before IR calibration
    state: WiimoteState = field(default_factory=WiimoteState)


# --------------------------------------------------------------------------
# Guided procedures
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    instruction: str
    mode: str  # "still": hold still while samples are averaged; "track": move, then click Done
    seconds: float = 1.5


class CalibrationError(Exception):
    pass


STILL_MAX_STD_ACCEL = 4.0  # raw 10-bit counts
STILL_MAX_STD_STICK = 3.0  # raw 8-bit counts
MIN_SAMPLES = 8


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values)


def _require_still(samples: Sequence[Sequence[float]], max_std: float, what: str) -> None:
    if len(samples) < MIN_SAMPLES:
        raise CalibrationError(f"Not enough data from the {what}. Is it connected?")
    for axis in zip(*samples):
        if statistics.pstdev(axis) > max_std:
            raise CalibrationError(f"The {what} moved during the measurement. Keep it still and try again.")


class Calibrator:
    """Base class for a guided procedure."""

    key = ""
    title = ""
    description = ""
    steps: list[Step] = []
    visual = "none"  # which live visual the UI shows: "accel", "stick", "classic", "ir", "none"

    def __init__(self) -> None:
        self.step = 0
        self._samples: list = []

    def unavailable_reason(self, raw: RawSnapshot | None) -> str | None:
        return None

    def start_step(self) -> None:
        self._samples = []

    def feed(self, raw: RawSnapshot) -> None:
        sample = self._sample(raw)
        if sample is not None:
            self._samples.append(sample)

    def _sample(self, raw: RawSnapshot):
        raise NotImplementedError

    def finish_step(self) -> bool:
        """Validate the step just captured; raises CalibrationError. Returns True when all steps are done."""
        self._finish(self.step, self._samples)
        self.step += 1
        self._samples = []
        return self.step >= len(self.steps)

    def _finish(self, step: int, samples: list) -> None:
        raise NotImplementedError

    def apply(self, cal: DeviceCalibration) -> DeviceCalibration:
        """Merge the result into an existing device calibration."""
        raise NotImplementedError

    def reset(self, cal: DeviceCalibration) -> DeviceCalibration:
        """Remove this procedure's override (back to factory)."""
        raise NotImplementedError

    def summary(self) -> str:
        return ""


class AccelCalibrator(Calibrator):
    """Three resting poses give zero-g and one-g for every axis.

    flat    -> X, Y horizontal, Z carries gravity
    upright -> X, Z horizontal, Y carries gravity
    side    -> Y, Z horizontal, X carries gravity
    """

    visual = "accel"
    POSES = ("flat", "upright", "side")
    GRAVITY_AXIS = {"flat": 2, "upright": 1, "side": 0}

    def __init__(self, device: str, reference: AccelCalibration):
        super().__init__()
        self.device = device
        self.reference = reference
        self.key = f"{device}_accel"
        self._means: dict[str, tuple[float, float, float]] = {}
        self.result: AccelCalibration | None = None
        if device == "wiimote":
            self.title = "Wiimote accelerometer"
            self.description = (
                "Measures the motion sensor's resting values in three positions. Improves tilt controls, "
                "tilt gestures and shake detection. Put the remote on a flat, stable surface for each step."
            )
            self.steps = [
                Step("Lay the Wiimote flat on a table, buttons facing up. Don't touch it.", "still"),
                Step("Stand the Wiimote on its end: IR window (front) pointing at the ceiling. Lean it "
                     "against something if needed, then let go.", "still"),
                Step("Lay the Wiimote on its side (left or right side down). Don't touch it.", "still"),
            ]
        else:
            self.title = "Nunchuk accelerometer"
            self.description = (
                "Measures the Nunchuk motion sensor in three positions. Improves Nunchuk tilt and shake."
            )
            self.steps = [
                Step("Lay the Nunchuk flat on a table, stick facing up. Don't touch it.", "still"),
                Step("Stand the Nunchuk on its cable end, C/Z buttons pointing at the ceiling.", "still"),
                Step("Lay the Nunchuk on its side (left or right). Don't touch it.", "still"),
            ]

    def unavailable_reason(self, raw: RawSnapshot | None) -> str | None:
        if raw is None:
            return "No data yet from this Wiimote."
        if self.device == "nunchuk" and raw.nunchuk is None:
            return "Plug in a Nunchuk to calibrate it."
        return None

    def _sample(self, raw: RawSnapshot):
        if self.device == "wiimote":
            return raw.accel
        return raw.nunchuk.accel if raw.nunchuk else None

    def _finish(self, step: int, samples: list) -> None:
        what = "Wiimote" if self.device == "wiimote" else "Nunchuk"
        _require_still(samples, STILL_MAX_STD_ACCEL, what)
        mean = tuple(_mean(axis) for axis in zip(*samples))
        pose = self.POSES[step]
        g = self.reference.normalize(mean)
        gaxis = self.GRAVITY_AXIS[pose]
        others = [abs(g[i]) for i in range(3) if i != gaxis]
        if abs(g[gaxis]) < 0.7 or max(others) > 0.4:
            raise CalibrationError(
                f"The {what} doesn't seem to be in the requested position "
                f"(measured {g[0]:+.2f}, {g[1]:+.2f}, {g[2]:+.2f} g). Check its position and try again."
            )
        self._means[pose] = mean  # type: ignore[assignment]
        if step == len(self.steps) - 1:
            self.result = self._compute()

    def _compute(self) -> AccelCalibration:
        f, u, s = self._means["flat"], self._means["upright"], self._means["side"]
        zero = ((f[0] + u[0]) / 2, (f[1] + s[1]) / 2, (u[2] + s[2]) / 2)
        gravity = (s[0], u[1], f[2])
        one = tuple(z + abs(gv - z) for z, gv in zip(zero, gravity))
        zi = tuple(int(round(v)) for v in zero)
        oi = tuple(int(round(v)) for v in one)
        if any(o - z < 20 for z, o in zip(zi, oi)):
            raise CalibrationError("The measured sensitivity is implausibly low. Please repeat the calibration.")
        return AccelCalibration(zero=zi, one_g=oi)  # type: ignore[arg-type]

    def apply(self, cal: DeviceCalibration) -> DeviceCalibration:
        assert self.result is not None
        field_name = "accel" if self.device == "wiimote" else "nunchuk_accel"
        return replace(cal, **{field_name: self.result})

    def reset(self, cal: DeviceCalibration) -> DeviceCalibration:
        return replace(cal, **{"accel" if self.device == "wiimote" else "nunchuk_accel": None})

    def summary(self) -> str:
        if self.result is None:
            return ""
        return f"zero = {self.result.zero}, one g = {self.result.one_g}"


MIN_STICK_HALF_SPAN = 25  # 8-bit counts each side of centre


class _StickTracker:
    def __init__(self) -> None:
        self.center: tuple[int, int] | None = None
        self.lo = [255, 255]
        self.hi = [0, 0]

    def track(self, xy: tuple[int, int]) -> None:
        for i in (0, 1):
            self.lo[i] = min(self.lo[i], xy[i])
            self.hi[i] = max(self.hi[i], xy[i])

    def axes(self, name: str) -> tuple[Axis, Axis]:
        assert self.center is not None
        axes = []
        for i, axis_name in enumerate("XY"):
            c = self.center[i]
            if self.hi[i] - c < MIN_STICK_HALF_SPAN or c - self.lo[i] < MIN_STICK_HALF_SPAN:
                raise CalibrationError(
                    f"{name}: the {axis_name} axis didn't move far enough. Rotate the stick all the way "
                    "around its edge a few times, then click Done."
                )
            axes.append((self.lo[i], c, self.hi[i]))
        return axes[0], axes[1]


class NunchukStickCalibrator(Calibrator):
    key = "nunchuk_stick"
    title = "Nunchuk stick"
    description = "Measures the stick's resting centre and how far it really travels, fixing drift and sticks that can't reach full deflection."
    visual = "stick"
    steps = [
        Step("Let go of the stick completely, then click Capture.", "still"),
        Step("Click Start, slowly rotate the stick around its outer edge 3 times, then click Done.", "track"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.tracker = _StickTracker()
        self.result: tuple[Axis, Axis] | None = None

    def unavailable_reason(self, raw: RawSnapshot | None) -> str | None:
        return None if raw is not None and raw.nunchuk is not None else "Plug in a Nunchuk to calibrate its stick."

    def _sample(self, raw: RawSnapshot):
        if raw.nunchuk is None:
            return None
        if self.step == 1:
            self.tracker.track(raw.nunchuk.stick)
        return raw.nunchuk.stick

    def _finish(self, step: int, samples: list) -> None:
        if step == 0:
            _require_still(samples, STILL_MAX_STD_STICK, "stick")
            self.tracker.center = tuple(int(round(_mean(a))) for a in zip(*samples))  # type: ignore[assignment]
        else:
            if not samples:
                raise CalibrationError("No stick movement was recorded.")
            self.result = self.tracker.axes("Nunchuk stick")

    def apply(self, cal: DeviceCalibration) -> DeviceCalibration:
        return replace(cal, nunchuk_stick=self.result)

    def reset(self, cal: DeviceCalibration) -> DeviceCalibration:
        return replace(cal, nunchuk_stick=None)

    def summary(self) -> str:
        if not self.result:
            return ""
        x, y = self.result
        return f"X {x[0]}–{x[1]}–{x[2]}, Y {y[0]}–{y[1]}–{y[2]} (min–centre–max)"


class ClassicCalibrator(Calibrator):
    key = "classic"
    title = "Classic Controller"
    description = "Calibrates both sticks (centre and range) and the analog L/R trigger travel."
    visual = "classic"
    steps = [
        Step("Let go of both sticks and both triggers, then click Capture.", "still"),
        Step("Click Start, rotate BOTH sticks around their outer edges 3 times and press L and R all the way "
             "down a few times, then click Done.", "track"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.left = _StickTracker()
        self.right = _StickTracker()
        self.trig_rest = (0, 0)
        self.trig_max = [0, 0]
        self.result: tuple | None = None
        self.triggers_kept_default = False

    def unavailable_reason(self, raw: RawSnapshot | None) -> str | None:
        return None if raw is not None and raw.classic is not None else "Plug in a Classic Controller to calibrate it."

    def _sample(self, raw: RawSnapshot):
        c = raw.classic
        if c is None:
            return None
        if self.step == 1:
            self.left.track(c.left)
            self.right.track(c.right)
            self.trig_max = [max(self.trig_max[0], c.lt), max(self.trig_max[1], c.rt)]
        return (*c.left, *c.right, c.lt, c.rt)

    def _finish(self, step: int, samples: list) -> None:
        if step == 0:
            # Classic sticks are coarse (6/5-bit), so allow one raw step of noise.
            _require_still([s[:4] for s in samples], 8.5, "sticks")
            means = [int(round(_mean(a))) for a in zip(*samples)]
            self.left.center = (means[0], means[1])
            self.right.center = (means[2], means[3])
            self.trig_rest = (max(s[4] for s in samples), max(s[5] for s in samples))
            return
        if not samples:
            raise CalibrationError("No movement was recorded.")
        left = self.left.axes("Left stick")
        right = self.right.axes("Right stick")
        lt = (self.trig_rest[0], self.trig_max[0])
        rt = (self.trig_rest[1], self.trig_max[1])
        triggers = None
        if lt[1] - lt[0] >= 10 and rt[1] - rt[0] >= 10:
            triggers = (lt, rt)
        self.triggers_kept_default = triggers is None
        self.result = (left, right, triggers)

    def apply(self, cal: DeviceCalibration) -> DeviceCalibration:
        assert self.result is not None
        left, right, triggers = self.result
        return replace(cal, classic_left=left, classic_right=right, classic_triggers=triggers or cal.classic_triggers)

    def reset(self, cal: DeviceCalibration) -> DeviceCalibration:
        return replace(cal, classic_left=None, classic_right=None, classic_triggers=None)

    def summary(self) -> str:
        if not self.result:
            return ""
        left, right, triggers = self.result
        trig = (f"L {triggers[0][0]}–{triggers[0][1]}, R {triggers[1][0]}–{triggers[1][1]}" if triggers
                else "triggers not analog or not fully pressed, kept default")
        return f"Left stick X {left[0]}, Y {left[1]}; right stick X {right[0]}, Y {right[1]}; {trig}"


class IrCalibrator(Calibrator):
    key = "ir"
    title = "IR pointer"
    description = (
        "Teaches the app where your screen is relative to the sensor bar, so the pointer covers exactly the "
        "screen. Sit where you normally play, with the sensor bar powered on."
    )
    visual = "ir"
    steps = [
        Step("Aim at the CENTRE of the screen and hold steady, then click Capture.", "still", 1.5),
        Step("Aim at the TOP-LEFT corner of the screen and hold steady, then click Capture.", "still", 1.5),
        Step("Aim at the BOTTOM-RIGHT corner of the screen and hold steady, then click Capture.", "still", 1.5),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._points: list[Vec2] = []
        self.result: IrCalibration | None = None

    def unavailable_reason(self, raw: RawSnapshot | None) -> str | None:
        if raw is None:
            return "No data yet from this Wiimote."
        return None

    def _sample(self, raw: RawSnapshot):
        return raw.pointer_raw if raw.pointer_raw is not None else "lost"

    def _finish(self, step: int, samples: list) -> None:
        seen = [s for s in samples if s != "lost"]
        if len(samples) < MIN_SAMPLES or len(seen) < 0.7 * len(samples):
            raise CalibrationError(
                "The sensor bar wasn't visible most of the time. Check that it is powered, and that the "
                "remote is 1–3 m away and pointing at it."
            )
        for axis in zip(*seen):
            if statistics.pstdev(axis) > 0.05:
                raise CalibrationError("The pointer moved too much. Rest your arm and hold steady.")
        self._points.append((_mean([p[0] for p in seen]), _mean([p[1] for p in seen])))
        if step == 2:
            center, tl, br = self._points
            hx = (br[0] - tl[0]) / 2
            hy = (tl[1] - br[1]) / 2
            if abs(hx) < 0.05 or abs(hy) < 0.05:
                raise CalibrationError(
                    "The corners are too close together. Aim exactly at the corners (move further from "
                    "the screen if the pointer leaves the sensor bar's view)."
                )
            self.result = IrCalibration(center=center, half_range=(hx, hy))

    def start_step(self) -> None:
        super().start_step()
        del self._points[self.step:]

    def apply(self, cal: DeviceCalibration) -> DeviceCalibration:
        return replace(cal, ir=self.result)

    def reset(self, cal: DeviceCalibration) -> DeviceCalibration:
        return replace(cal, ir=None)

    def summary(self) -> str:
        if not self.result:
            return ""
        c, h = self.result.center, self.result.half_range
        return f"centre ({c[0]:+.2f}, {c[1]:+.2f}), half-range ({h[0]:+.2f}, {h[1]:+.2f})"


class DeadzoneCalibrator(Calibrator):
    """Measures stick noise at rest and suggests deadzones (applied to the
    active *profile*, not stored as device calibration)."""

    key = "deadzone"
    title = "Deadzone helper"
    description = (
        "Measures how much each stick wobbles when untouched and suggests the smallest deadzone that stops "
        "drift. Run it after calibrating the sticks. The result is applied to the active profile."
    )
    visual = "none"
    steps = [Step("Let go of all sticks, then click Capture. Measuring takes 3 seconds.", "still", 3.0)]

    SOURCES = (("nunchuk.stick", "Nunchuk stick"), ("classic.left_stick", "Classic left stick"),
               ("classic.right_stick", "Classic right stick"))

    def __init__(self) -> None:
        super().__init__()
        self.suggestions: dict[str, float] = {}

    def unavailable_reason(self, raw: RawSnapshot | None) -> str | None:
        if raw is None or (raw.state.nunchuk is None and raw.state.classic is None):
            return "Plug in a Nunchuk or Classic Controller to measure stick noise."
        return None

    def _sample(self, raw: RawSnapshot):
        st = raw.state
        return {
            "nunchuk.stick": st.nunchuk.stick if st.nunchuk else None,
            "classic.left_stick": st.classic.left if st.classic else None,
            "classic.right_stick": st.classic.right if st.classic else None,
        }

    def _finish(self, step: int, samples: list) -> None:
        if len(samples) < MIN_SAMPLES:
            raise CalibrationError("Not enough data. Is the extension connected?")
        self.suggestions = {}
        for source, _label in self.SOURCES:
            values = [s[source] for s in samples if s[source] is not None]
            if len(values) >= MIN_SAMPLES:
                worst = max(math.hypot(x, y) for x, y in values)
                if worst > 0.6:
                    raise CalibrationError("A stick was being held. Let go of all sticks and try again.")
                self.suggestions[source] = round(min(0.4, max(0.03, worst * 1.25 + 0.03)), 2)
        if not self.suggestions:
            raise CalibrationError("No stick data was received.")

    def apply_to_profile(self, profile) -> list[str]:
        """Set the suggested deadzones on every stick output using a measured source."""
        changes = []
        for attr, label in (("left_stick", "left thumbstick"), ("right_stick", "right thumbstick"), ("mouse", "mouse")):
            cfg = getattr(profile, attr)
            dz = self.suggestions.get(cfg.source)
            if dz is not None:
                cfg.deadzone = dz
                changes.append(f"{label}: {dz:.0%}")
        return changes

    def summary(self) -> str:
        names = dict(self.SOURCES)
        return ", ".join(f"{names[s]}: {dz:.0%}" for s, dz in self.suggestions.items())
