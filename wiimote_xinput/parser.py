"""Pure-Python parsing of Wiimote HID input reports.

Nothing in this module touches hardware: every function takes raw bytes and
returns immutable dataclasses, which keeps it fast and unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence, Union

from .constants import (
    BATTERY_FULL_RAW,
    CLASSIC_BUTTON_MASK,
    CORE_BUTTON_MASK,
    IR_MAX_X,
    IR_MAX_Y,
    NUN_C,
    NUN_Z,
    REPORT_LAYOUTS,
    InputReport,
)

Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]


def _clamp(v: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


# --------------------------------------------------------------------------
# Calibration
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class AccelCalibration:
    """Zero-g and one-g raw readings (10-bit) for each accelerometer axis."""

    zero: tuple[int, int, int] = (512, 512, 512)
    one_g: tuple[int, int, int] = (616, 616, 616)

    @classmethod
    def from_bytes(cls, data: Sequence[int]) -> "AccelCalibration | None":
        """Decode the common 8-byte layout used by the Wiimote EEPROM and the
        Nunchuk: X0 Y0 Z0 LSB XG YG ZG LSB (upper 8 bits + packed 2-bit LSBs).
        """
        if len(data) < 8:
            return None

        def unpack(base: int) -> tuple[int, int, int]:
            lsb = data[base + 3]
            return (
                (data[base] << 2) | ((lsb >> 4) & 0x03),
                (data[base + 1] << 2) | ((lsb >> 2) & 0x03),
                (data[base + 2] << 2) | (lsb & 0x03),
            )

        zero, one = unpack(0), unpack(4)
        # Reject obviously broken calibration (blank EEPROM, clone devices).
        if any(o - z < 20 for z, o in zip(zero, one)):
            return None
        return cls(zero=zero, one_g=one)

    @classmethod
    def from_eeprom(cls, data: Sequence[int]) -> "AccelCalibration | None":
        """Decode the 10-byte block at EEPROM 0x0016 and verify its checksum."""
        if len(data) < 10:
            return None
        if (sum(data[0:9]) + 0x55) & 0xFF != data[9]:
            return None
        return cls.from_bytes(data[0:8])

    def normalize(self, raw: Sequence[int]) -> Vec3:
        return tuple(  # type: ignore[return-value]
            (r - z) / float(o - z) for r, z, o in zip(raw, self.zero, self.one_g)
        )


NUNCHUK_DEFAULT_ACCEL = AccelCalibration(zero=(512, 512, 512), one_g=(716, 716, 716))


@dataclass
class AxisCalibration:
    """Min/centre/max calibration for one analog axis.

    When ``learn`` is enabled the range grows to cover any value actually
    observed, which fixes third-party sticks with bad or missing calibration.
    """

    minimum: int
    center: int
    maximum: int
    learn: bool = True

    def is_sane(self, min_span: int = 10) -> bool:
        return (
            self.minimum + min_span <= self.center <= self.maximum - min_span
        )

    def normalize(self, raw: int) -> float:
        if self.learn:
            if raw < self.minimum:
                self.minimum = raw
            elif raw > self.maximum:
                self.maximum = raw
        if raw >= self.center:
            span = self.maximum - self.center
        else:
            span = self.center - self.minimum
        if span <= 0:
            return 0.0
        return _clamp((raw - self.center) / span)


@dataclass
class NunchukCalibration:
    accel: AccelCalibration = NUNCHUK_DEFAULT_ACCEL
    stick_x: AxisCalibration = field(default_factory=lambda: AxisCalibration(35, 128, 221))
    stick_y: AxisCalibration = field(default_factory=lambda: AxisCalibration(35, 128, 221))

    @classmethod
    def from_bytes(cls, data: Sequence[int]) -> "NunchukCalibration":
        """Decode the 16 bytes at 0xA40020. Falls back to defaults per field
        when values are implausible (common with third-party nunchuks)."""
        cal = cls()
        if len(data) < 14:
            return cal
        accel = AccelCalibration.from_bytes(data[0:8])
        if accel is not None:
            cal.accel = accel
        sx = AxisCalibration(minimum=data[9], center=data[10], maximum=data[8])
        sy = AxisCalibration(minimum=data[12], center=data[13], maximum=data[11])
        if sx.is_sane():
            cal.stick_x = sx
        if sy.is_sane():
            cal.stick_y = sy
        return cal


@dataclass
class ClassicCalibration:
    """All values are stored in 8-bit scale (left stick raw << 2, right << 3)."""

    lx: AxisCalibration = field(default_factory=lambda: AxisCalibration(28, 128, 228))
    ly: AxisCalibration = field(default_factory=lambda: AxisCalibration(28, 128, 228))
    rx: AxisCalibration = field(default_factory=lambda: AxisCalibration(28, 128, 228))
    ry: AxisCalibration = field(default_factory=lambda: AxisCalibration(28, 128, 228))

    @classmethod
    def from_bytes(cls, data: Sequence[int]) -> "ClassicCalibration":
        cal = cls()
        if len(data) < 12:
            return cal
        names = ("lx", "ly", "rx", "ry")
        for i, name in enumerate(names):
            mx, mn, ctr = data[i * 3], data[i * 3 + 1], data[i * 3 + 2]
            axis = AxisCalibration(minimum=mn, center=ctr, maximum=mx)
            if axis.is_sane():
                setattr(cal, name, axis)
        return cal


# --------------------------------------------------------------------------
# Parsed report types
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class StatusReport:
    buttons: int
    battery_raw: int
    battery_low: bool
    extension_connected: bool
    speaker_enabled: bool
    ir_enabled: bool
    leds: int

    @property
    def battery_percent(self) -> int:
        return max(0, min(100, round(self.battery_raw * 100 / BATTERY_FULL_RAW)))


@dataclass(frozen=True)
class MemoryReadResponse:
    buttons: int
    size: int
    error: int
    address: int  # low 16 bits of the address of the first byte in this chunk
    data: bytes


@dataclass(frozen=True)
class Acknowledge:
    buttons: int
    report: int
    error: int


@dataclass(frozen=True)
class DataReport:
    report_id: int
    buttons: int | None
    accel_raw: tuple[int, int, int] | None
    ir_raw: bytes | None
    ext_raw: bytes | None


ParsedReport = Union[StatusReport, MemoryReadResponse, Acknowledge, DataReport]


@dataclass(frozen=True)
class NunchukState:
    stick: Vec2 = (0.0, 0.0)
    buttons: int = 0
    accel: Vec3 | None = None

    @property
    def c(self) -> bool:
        return bool(self.buttons & NUN_C)

    @property
    def z(self) -> bool:
        return bool(self.buttons & NUN_Z)


@dataclass(frozen=True)
class ClassicState:
    left: Vec2 = (0.0, 0.0)
    right: Vec2 = (0.0, 0.0)
    lt: float = 0.0
    rt: float = 0.0
    buttons: int = 0


@dataclass(frozen=True)
class IrDot:
    x: int
    y: int
    size: int | None = None


# --------------------------------------------------------------------------
# Parsing functions
# --------------------------------------------------------------------------


def parse_buttons(b0: int, b1: int) -> int:
    return ((b0 << 8) | b1) & CORE_BUTTON_MASK


def parse_accel_raw(payload: Sequence[int], offset: int) -> tuple[int, int, int]:
    """Decode 10-bit X / 9-bit Y,Z accelerometer values (LSBs live in the
    button bytes)."""
    b0, b1 = payload[0], payload[1]
    x = (payload[offset] << 2) | ((b0 >> 5) & 0x03)
    y = (payload[offset + 1] << 2) | ((b1 >> 4) & 0x02)
    z = (payload[offset + 2] << 2) | ((b1 >> 5) & 0x02)
    return x, y, z


def parse_input_report(report: Sequence[int]) -> ParsedReport | None:
    """Parse one raw input report (first byte = report ID)."""
    if not report:
        return None
    rid = report[0]
    p = bytes(report[1:])

    if rid == InputReport.STATUS and len(p) >= 6:
        flags = p[2]
        return StatusReport(
            buttons=parse_buttons(p[0], p[1]),
            battery_raw=p[5],
            battery_low=bool(flags & 0x01),
            extension_connected=bool(flags & 0x02),
            speaker_enabled=bool(flags & 0x04),
            ir_enabled=bool(flags & 0x08),
            leds=flags & 0xF0,
        )

    if rid == InputReport.READ_MEMORY_DATA and len(p) >= 5:
        se = p[2]
        size = ((se >> 4) & 0x0F) + 1
        return MemoryReadResponse(
            buttons=parse_buttons(p[0], p[1]),
            size=size,
            error=se & 0x0F,
            address=(p[3] << 8) | p[4],
            data=bytes(p[5 : 5 + size]),
        )

    if rid == InputReport.ACKNOWLEDGE and len(p) >= 4:
        return Acknowledge(buttons=parse_buttons(p[0], p[1]), report=p[2], error=p[3])

    layout = REPORT_LAYOUTS.get(rid)
    if layout is None:
        return None
    needed = max(
        2 if layout.has_buttons else 0,
        (layout.accel_offset or 0) + (3 if layout.accel_offset is not None else 0),
        (layout.ir_offset or 0) + layout.ir_length,
        (layout.ext_offset or 0) + layout.ext_length,
    )
    if len(p) < needed:
        return None
    buttons = parse_buttons(p[0], p[1]) if layout.has_buttons else None
    accel = parse_accel_raw(p, layout.accel_offset) if layout.accel_offset is not None else None
    ir = p[layout.ir_offset : layout.ir_offset + layout.ir_length] if layout.ir_offset is not None else None
    ext = p[layout.ext_offset : layout.ext_offset + layout.ext_length] if layout.ext_offset is not None else None
    return DataReport(report_id=rid, buttons=buttons, accel_raw=accel, ir_raw=ir, ext_raw=ext)


def parse_nunchuk(ext: Sequence[int], cal: NunchukCalibration) -> NunchukState | None:
    if len(ext) < 6:
        return None
    if all(b == 0xFF for b in ext[:6]):
        return None  # extension yanked / not initialised
    b5 = ext[5]
    stick = (cal.stick_x.normalize(ext[0]), cal.stick_y.normalize(ext[1]))
    raw_accel = (
        (ext[2] << 2) | ((b5 >> 2) & 0x03),
        (ext[3] << 2) | ((b5 >> 4) & 0x03),
        (ext[4] << 2) | ((b5 >> 6) & 0x03),
    )
    buttons = 0
    if not b5 & 0x01:
        buttons |= NUN_Z
    if not b5 & 0x02:
        buttons |= NUN_C
    return NunchukState(stick=stick, buttons=buttons, accel=cal.accel.normalize(raw_accel))


def parse_classic(ext: Sequence[int], cal: ClassicCalibration) -> ClassicState | None:
    if len(ext) < 6:
        return None
    if all(b == 0xFF for b in ext[:6]):
        return None
    b0, b1, b2, b3, b4, b5 = ext[:6]
    lx = b0 & 0x3F
    ly = b1 & 0x3F
    rx = ((b0 >> 3) & 0x18) | ((b1 >> 5) & 0x06) | ((b2 >> 7) & 0x01)
    ry = b2 & 0x1F
    lt = ((b2 >> 2) & 0x18) | ((b3 >> 5) & 0x07)
    rt = b3 & 0x1F
    buttons = (~((b4 << 8) | b5)) & CLASSIC_BUTTON_MASK
    return ClassicState(
        left=(cal.lx.normalize(lx << 2), cal.ly.normalize(ly << 2)),
        right=(cal.rx.normalize(rx << 3), cal.ry.normalize(ry << 3)),
        lt=lt / 31.0,
        rt=rt / 31.0,
        buttons=buttons,
    )


def parse_ir(data: Sequence[int]) -> list[IrDot]:
    """Decode basic (10-byte) or extended (12-byte) IR camera data."""
    dots: list[IrDot] = []
    if len(data) == 10:
        for g in (0, 5):
            b = data[g : g + 5]
            x1 = b[0] | (((b[2] >> 4) & 0x03) << 8)
            y1 = b[1] | (((b[2] >> 6) & 0x03) << 8)
            x2 = b[3] | ((b[2] & 0x03) << 8)
            y2 = b[4] | (((b[2] >> 2) & 0x03) << 8)
            for x, y in ((x1, y1), (x2, y2)):
                if x < 1023 or y < 1023:
                    dots.append(IrDot(x, y))
    elif len(data) == 12:
        for g in range(0, 12, 3):
            b = data[g : g + 3]
            x = b[0] | (((b[2] >> 4) & 0x03) << 8)
            y = b[1] | (((b[2] >> 6) & 0x03) << 8)
            if b[0] == 0xFF and b[1] == 0xFF and b[2] == 0xFF:
                continue
            dots.append(IrDot(x, y, b[2] & 0x0F))
    return dots


def ir_pointer(dots: Sequence[IrDot]) -> Vec2 | None:
    """Return a normalised pointer position (-1..1, +Y = up) from IR dots.

    The camera image is mirrored horizontally relative to where the remote
    points, hence the X inversion.
    """
    valid = [d for d in dots if d.x <= IR_MAX_X and d.y <= IR_MAX_Y]
    if not valid:
        return None
    if len(valid) >= 2:
        mx = (valid[0].x + valid[1].x) / 2.0
        my = (valid[0].y + valid[1].y) / 2.0
    else:
        mx, my = float(valid[0].x), float(valid[0].y)
    return (_clamp(1.0 - 2.0 * mx / IR_MAX_X), _clamp(2.0 * my / IR_MAX_Y - 1.0))


@dataclass(frozen=True)
class WiimoteState:
    """Fully decoded, calibrated snapshot of one Wiimote (plus extension)."""

    buttons: int = 0
    accel: Vec3 | None = None
    nunchuk: NunchukState | None = None
    classic: ClassicState | None = None
    ir_dots: tuple[IrDot, ...] = ()
    pointer: Vec2 | None = None
    motion: int = 0  # MOT_* gesture bits from motion.MotionDetector


EMPTY_STATE = WiimoteState()
