"""Turn accelerometer data into bindable "virtual buttons" (motion gestures).

* **Shake**: the measured acceleration differs from gravity (1 g) by more than
  a threshold. The gesture stays pressed for a short hold time after the last
  spike, so a quick waggle registers as one solid press instead of flickering.
* **Tilt left/right/up/down**: the remote is held at more than a set angle.
  Hysteresis (the release angle is a few degrees lower) prevents chatter near
  the threshold, and tilt is frozen while the remote is being swung, so
  shaking does not also fire random tilt directions.

Axis conventions follow wiibrew: at rest the accelerometer reads +1 g on the
axis pointing up. Held vertically (pointing at the screen), rolling the remote
to the right gives +X and pointing it upward gives +Y.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from .constants import (
    MOT_NUNCHUK_SHAKE,
    MOT_NUNCHUK_TILT_DOWN,
    MOT_NUNCHUK_TILT_LEFT,
    MOT_NUNCHUK_TILT_RIGHT,
    MOT_NUNCHUK_TILT_UP,
    MOT_WIIMOTE_SHAKE,
    MOT_WIIMOTE_TILT_DOWN,
    MOT_WIIMOTE_TILT_LEFT,
    MOT_WIIMOTE_TILT_RIGHT,
    MOT_WIIMOTE_TILT_UP,
)

Vec3 = tuple[float, float, float]

TILT_HYSTERESIS_DEG = 5.0
STEADY_MIN_G = 0.6  # tilt is only trusted while |accel| is close to 1 g
STEADY_MAX_G = 1.4


def tilt_angles(accel: Vec3, sideways: bool) -> tuple[float, float]:
    """(left/right, up/down) tilt in radians. Positive = right / up.

    Vertical: roll and pitch. Sideways (steering-wheel style, D-Pad on the
    left): rotation about the axis pointing at the screen, and forward tilt.
    """
    gx, gy, gz = accel
    if sideways:
        return math.atan2(gy, abs(gx) or 1e-6), math.atan2(gz, abs(gx) or 1e-6)
    return math.atan2(gx, abs(gz) or 1e-6), math.atan2(gy, abs(gz) or 1e-6)


@dataclass(frozen=True)
class _Bits:
    shake: int
    left: int
    right: int
    up: int
    down: int


WIIMOTE_BITS = _Bits(MOT_WIIMOTE_SHAKE, MOT_WIIMOTE_TILT_LEFT, MOT_WIIMOTE_TILT_RIGHT,
                     MOT_WIIMOTE_TILT_UP, MOT_WIIMOTE_TILT_DOWN)
NUNCHUK_BITS = _Bits(MOT_NUNCHUK_SHAKE, MOT_NUNCHUK_TILT_LEFT, MOT_NUNCHUK_TILT_RIGHT,
                     MOT_NUNCHUK_TILT_UP, MOT_NUNCHUK_TILT_DOWN)


class _GestureTracker:
    """Shake + tilt state for one accelerometer."""

    def __init__(self, bits: _Bits):
        self.bits = bits
        self.reset()

    def reset(self) -> None:
        self._shake_until = 0.0
        self._tilt = 0  # currently pressed tilt bits

    def update(self, accel: Vec3 | None, sideways: bool, cfg, now: float) -> int:
        if accel is None:
            self.reset()
            return 0
        b = self.bits
        mag = math.sqrt(accel[0] ** 2 + accel[1] ** 2 + accel[2] ** 2)
        out = 0

        if abs(mag - 1.0) >= cfg.shake_threshold_g:
            self._shake_until = now + cfg.shake_hold_s
        if now < self._shake_until:
            out |= b.shake

        if STEADY_MIN_G <= mag <= STEADY_MAX_G and now >= self._shake_until:
            press = math.radians(cfg.tilt_angle_deg)
            release = math.radians(max(1.0, cfg.tilt_angle_deg - TILT_HYSTERESIS_DEG))
            lr, ud = tilt_angles(accel, sideways)
            tilt = 0
            for value, neg_bit, pos_bit in ((lr, b.left, b.right), (ud, b.down, b.up)):
                if value >= press or (self._tilt & pos_bit and value >= release):
                    tilt |= pos_bit
                elif value <= -press or (self._tilt & neg_bit and value <= -release):
                    tilt |= neg_bit
            self._tilt = tilt
        out |= self._tilt
        return out


class MotionDetector:
    """Per-Wiimote gesture detector (Wiimote + Nunchuk accelerometers)."""

    def __init__(self) -> None:
        self._wiimote = _GestureTracker(WIIMOTE_BITS)
        self._nunchuk = _GestureTracker(NUNCHUK_BITS)

    def reset(self) -> None:
        self._wiimote.reset()
        self._nunchuk.reset()

    def update(self, wiimote_accel: Vec3 | None, nunchuk_accel: Vec3 | None,
               sideways: bool, cfg, now: float | None = None) -> int:
        """Return the MOT_* bitmask of gestures active right now."""
        if not cfg.enabled:
            return 0
        now = time.monotonic() if now is None else now
        return (self._wiimote.update(wiimote_accel, sideways, cfg, now)
                | self._nunchuk.update(nunchuk_accel, False, cfg, now))
