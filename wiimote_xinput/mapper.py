"""Translate a decoded :class:`WiimoteState` into an XInput gamepad state.

A :class:`Mapper` "compiles" a :class:`Profile` into flat lookup tables once,
so the per-report hot path is a handful of integer operations.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .config import Profile, StickConfig, TriggerConfig
from .constants import (
    BTN_DOWN,
    BTN_LEFT,
    BTN_RIGHT,
    BTN_UP,
    BUTTON_SOURCES,
    XINPUT_TARGETS,
)
from .parser import WiimoteState

Vec2 = tuple[float, float]


@dataclass(frozen=True)
class XInputState:
    """Mirror of XUSB_REPORT / XINPUT_GAMEPAD."""

    buttons: int = 0
    lt: int = 0  # 0..255
    rt: int = 0
    lx: int = 0  # -32768..32767, +Y is up
    ly: int = 0
    rx: int = 0
    ry: int = 0


NEUTRAL = XInputState()

_DPAD_MASK = BTN_UP | BTN_DOWN | BTN_LEFT | BTN_RIGHT


def rotate_dpad_sideways(buttons: int) -> int:
    """Held sideways (D-Pad on the left), the physical arrows rotate 90°:
    Up->Left, Down->Right, Left->Down, Right->Up."""
    out = buttons & ~_DPAD_MASK
    if buttons & BTN_UP:
        out |= BTN_LEFT
    if buttons & BTN_DOWN:
        out |= BTN_RIGHT
    if buttons & BTN_LEFT:
        out |= BTN_DOWN
    if buttons & BTN_RIGHT:
        out |= BTN_UP
    return out


def apply_stick_response(x: float, y: float, cfg: StickConfig) -> Vec2:
    """Radial deadzone + outer saturation + response curve + inversion."""
    if cfg.invert_x:
        x = -x
    if cfg.invert_y:
        y = -y
    mag = math.hypot(x, y)
    dz = cfg.deadzone
    if mag <= dz or mag == 0.0:
        return 0.0, 0.0
    outer = max(cfg.saturation, dz + 0.01)
    scaled = min(1.0, (mag - dz) / (outer - dz))
    if cfg.curve != 1.0:
        scaled = scaled**cfg.curve
    k = scaled / mag
    return max(-1.0, min(1.0, x * k)), max(-1.0, min(1.0, y * k))


def apply_trigger_response(v: float, cfg: TriggerConfig) -> float:
    if v <= cfg.deadzone:
        return 0.0
    return min(1.0, (v - cfg.deadzone) / (1.0 - cfg.deadzone))


def to_axis(v: float) -> int:
    return max(-32768, min(32767, int(round(v * 32767))))


def to_trigger(v: float) -> int:
    return max(0, min(255, int(round(v * 255))))


def tilt_to_stick(accel: tuple[float, float, float] | None, sideways: bool, range_deg: float) -> Vec2:
    """Convert gravity direction into a stick deflection.

    Vertical: roll -> X, pitch -> Y. Sideways (steering wheel style): rotation
    about the axis pointing at the screen -> X, forward tilt -> Y. Signs vary by
    how you hold the remote; use the invert toggles to taste.
    """
    if accel is None:
        return 0.0, 0.0
    gx, gy, gz = accel
    lim = math.radians(range_deg)
    if sideways:
        ax = math.atan2(gy, abs(gx) or 1e-6)
        ay = math.atan2(gz, abs(gx) or 1e-6)
    else:
        ax = math.atan2(gx, abs(gz) or 1e-6)
        ay = math.atan2(gy, abs(gz) or 1e-6)
    return max(-1.0, min(1.0, ax / lim)), max(-1.0, min(1.0, ay / lim))


def dpad_to_stick(buttons: int) -> Vec2:
    x = (1.0 if buttons & BTN_RIGHT else 0.0) - (1.0 if buttons & BTN_LEFT else 0.0)
    y = (1.0 if buttons & BTN_UP else 0.0) - (1.0 if buttons & BTN_DOWN else 0.0)
    if x and y:
        x *= 0.7071
        y *= 0.7071
    return x, y


class Mapper:
    def __init__(self, profile: Profile):
        self.set_profile(profile)

    def set_profile(self, profile: Profile) -> None:
        compiled: list[tuple[str, int, int, str | None]] = []
        for source, target in profile.buttons.items():
            if source not in BUTTON_SOURCES or target not in XINPUT_TARGETS:
                continue
            group, src_mask = BUTTON_SOURCES[source]
            tgt_mask, trigger = XINPUT_TARGETS[target]
            if tgt_mask or trigger:
                compiled.append((group, src_mask, tgt_mask, trigger))
        # Assigned last and as one object, so a concurrent map() call sees
        # either the old or the new profile, never a mix.
        self._compiled = (profile, tuple(compiled), profile.orientation == "sideways")

    @property
    def profile(self) -> Profile:
        return self._compiled[0]

    def _stick(self, state: WiimoteState, cfg: StickConfig, core: int, sideways: bool) -> Vec2:
        src = cfg.source
        if src == "nunchuk.stick":
            raw = state.nunchuk.stick if state.nunchuk else (0.0, 0.0)
        elif src == "classic.left_stick":
            raw = state.classic.left if state.classic else (0.0, 0.0)
        elif src == "classic.right_stick":
            raw = state.classic.right if state.classic else (0.0, 0.0)
        elif src == "wiimote.tilt":
            raw = tilt_to_stick(state.accel, sideways, cfg.tilt_range_deg)
        elif src == "nunchuk.tilt":
            raw = tilt_to_stick(state.nunchuk.accel if state.nunchuk else None, False, cfg.tilt_range_deg)
        elif src == "ir.pointer":
            raw = state.pointer or (0.0, 0.0)
        elif src == "wiimote.dpad":
            raw = dpad_to_stick(core)
        else:
            return 0.0, 0.0
        return apply_stick_response(raw[0], raw[1], cfg)

    @staticmethod
    def _analog_trigger(state: WiimoteState, cfg: TriggerConfig) -> float:
        if state.classic is None:
            return 0.0
        if cfg.source == "classic.lt":
            return apply_trigger_response(state.classic.lt, cfg)
        if cfg.source == "classic.rt":
            return apply_trigger_response(state.classic.rt, cfg)
        return 0.0

    def map(self, state: WiimoteState) -> XInputState:
        profile, compiled, sideways = self._compiled

        core = state.buttons
        if sideways:
            core = rotate_dpad_sideways(core)
        groups = {
            "core": core,
            "nunchuk": state.nunchuk.buttons if state.nunchuk else 0,
            "classic": state.classic.buttons if state.classic else 0,
        }

        buttons = 0
        lt_digital = rt_digital = False
        for group, src_mask, tgt_mask, trigger in compiled:
            if groups[group] & src_mask:
                buttons |= tgt_mask
                if trigger == "lt":
                    lt_digital = True
                elif trigger == "rt":
                    rt_digital = True

        lx, ly = self._stick(state, profile.left_stick, core, sideways)
        rx, ry = self._stick(state, profile.right_stick, core, sideways)

        if (
            profile.classic_left_stick_fallback
            and state.classic is not None
            and profile.left_stick.source != "classic.left_stick"
            and profile.right_stick.source != "classic.left_stick"
        ):
            cx, cy = apply_stick_response(*state.classic.left, profile.left_stick)
            if math.hypot(cx, cy) > math.hypot(lx, ly):
                lx, ly = cx, cy

        lt = 1.0 if lt_digital else self._analog_trigger(state, profile.left_trigger)
        rt = 1.0 if rt_digital else self._analog_trigger(state, profile.right_trigger)

        return XInputState(
            buttons=buttons,
            lt=to_trigger(lt),
            rt=to_trigger(rt),
            lx=to_axis(lx),
            ly=to_axis(ly),
            rx=to_axis(rx),
            ry=to_axis(ry),
        )
