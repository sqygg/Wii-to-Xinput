"""A protocol-level Wiimote emulator.

``SimulatedTransport`` speaks the same byte protocol as a real Wiimote
(status, memory reads/writes, acks, reporting modes), so the full session
stack can be exercised without hardware: ``python -m wiimote_xinput --simulate``.
"""

from __future__ import annotations

import math
import threading
import time
from collections import deque

from .constants import (
    BTN_A,
    BTN_B,
    BTN_DOWN,
    BTN_LEFT,
    BTN_RIGHT,
    BTN_UP,
    EEPROM_ACCEL_CALIBRATION,
    MODE_CONTINUOUS_FLAG,
    REG_EXT_CALIBRATION,
    REG_EXT_ID,
    REPORT_LAYOUTS,
    InputReport,
    OutputReport,
)
from .hid_transport import DeviceDisconnected, WiimoteHidInfo

NUNCHUK_ID = bytes([0x00, 0x00, 0xA4, 0x20, 0x00, 0x00])
CLASSIC_ID = bytes([0x00, 0x00, 0xA4, 0x20, 0x01, 0x01])


def _eeprom_accel_block(zero: int = 0x80, one_g: int = 0x9A) -> bytes:
    data = [zero, zero, zero, 0x00, one_g, one_g, one_g, 0x00, 0x40]
    return bytes(data + [(sum(data) + 0x55) & 0xFF])


def nunchuk_calibration_block() -> bytes:
    data = [0x80, 0x80, 0x80, 0x00, 0xB3, 0xB3, 0xB3, 0x00, 0xE0, 0x20, 0x80, 0xE0, 0x20, 0x80]
    s = sum(data)
    return bytes(data + [(s + 0x55) & 0xFF, (s + 0xAA) & 0xFF])


def encode_buttons(buttons: int, accel: tuple[int, int, int] = (512, 512, 512)) -> bytes:
    b0 = (buttons >> 8) & 0x1F | ((accel[0] & 0x03) << 5)
    b1 = buttons & 0x9F | ((accel[1] & 0x02) << 4) | ((accel[2] & 0x02) << 5)
    return bytes([b0, b1])


def encode_accel(accel: tuple[int, int, int]) -> bytes:
    return bytes([(accel[0] >> 2) & 0xFF, (accel[1] >> 2) & 0xFF, (accel[2] >> 2) & 0xFF])


def encode_nunchuk(sx: int, sy: int, accel: tuple[int, int, int], c: bool, z: bool) -> bytes:
    b5 = (0 if z else 0x01) | (0 if c else 0x02)
    b5 |= (accel[0] & 0x03) << 2 | (accel[1] & 0x03) << 4 | (accel[2] & 0x03) << 6
    return bytes([sx, sy, accel[0] >> 2, accel[1] >> 2, accel[2] >> 2, b5])


def encode_classic(lx: int, ly: int, rx: int, ry: int, lt: int, rt: int, buttons: int) -> bytes:
    """lx/ly 0..63, rx/ry/lt/rt 0..31, buttons = active-high CC_* mask."""
    b0 = (lx & 0x3F) | ((rx & 0x18) << 3)
    b1 = (ly & 0x3F) | ((rx & 0x06) << 5)
    b2 = (ry & 0x1F) | ((rx & 0x01) << 7) | ((lt & 0x18) << 2)
    b3 = (rt & 0x1F) | ((lt & 0x07) << 5)
    raw = (~buttons) & 0xFFFF
    return bytes([b0, b1, b2, b3, (raw >> 8) & 0xFF, raw & 0xFF])


class SimulatedTransport:
    """Emulates a Wiimote with a Nunchuk attached, producing smooth demo input."""

    REPORT_INTERVAL_S = 0.01  # ~100 Hz, like real hardware in continuous mode

    def __init__(self, extension_id: bytes | None = NUNCHUK_ID, battery_raw: int = 0xA0):
        self.extension_id = extension_id
        self.battery_raw = battery_raw
        self.mode = 0x30
        self.continuous = False
        self.leds = 0
        self.rumble = False
        self.connected = True
        self.writes: list[bytes] = []
        self._queue: deque[bytes] = deque()
        self._lock = threading.Lock()
        self._next_report = 0.0
        self._t0 = time.monotonic()
        self._opened = False

    # -- Transport protocol ----------------------------------------------------

    def open(self) -> None:
        if not self.connected:
            raise DeviceDisconnected("simulated device is off")
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def write(self, report: bytes) -> None:
        if not self.connected or not self._opened:
            raise DeviceDisconnected("simulated device is off")
        self.writes.append(bytes(report))
        rid, p = report[0], report[1:]
        self.rumble = bool(p[0] & 0x01) if p else False
        with self._lock:
            if rid == OutputReport.STATUS_REQUEST:
                self._queue.append(self._status())
            elif rid == OutputReport.LEDS:
                self.leds = p[0] & 0xF0
            elif rid == OutputReport.DATA_REPORTING_MODE:
                self.continuous = bool(p[0] & MODE_CONTINUOUS_FLAG)
                self.mode = p[1]
            elif rid == OutputReport.WRITE_MEMORY:
                self._queue.append(self._ack(rid, 0))
            elif rid == OutputReport.READ_MEMORY:
                self._handle_read(p)
            elif rid in (OutputReport.IR_CAMERA_ENABLE, OutputReport.IR_CAMERA_ENABLE_2):
                pass

    def read(self, timeout_ms: int) -> bytes | None:
        if not self.connected or not self._opened:
            raise DeviceDisconnected("simulated device is off")
        with self._lock:
            if self._queue:
                return self._queue.popleft()
        now = time.monotonic()
        if self.mode != 0x30 or self.continuous:
            if now < self._next_report:
                wait = min(self._next_report - now, timeout_ms / 1000.0)
                time.sleep(max(0.0, wait))
                if time.monotonic() < self._next_report:
                    return None
            self._next_report = time.monotonic() + self.REPORT_INTERVAL_S
            return self._data_report(time.monotonic() - self._t0)
        time.sleep(timeout_ms / 1000.0)
        return None

    # -- helpers -------------------------------------------------------------

    def _status(self) -> bytes:
        flags = self.leds | (0x02 if self.extension_id else 0)
        if self.battery_raw < 0x20:
            flags |= 0x01
        # A status report resets the reporting mode, exactly like hardware.
        self.mode, self.continuous = 0x30, False
        return bytes([InputReport.STATUS, 0, 0, flags, 0, 0, self.battery_raw])

    @staticmethod
    def _ack(report: int, error: int) -> bytes:
        return bytes([InputReport.ACKNOWLEDGE, 0, 0, report, error])

    def _handle_read(self, p: bytes) -> None:
        space = p[0]
        addr = (p[1] << 16) | (p[2] << 8) | p[3]
        size = (p[4] << 8) | p[5]
        if space == 0x00 and addr == EEPROM_ACCEL_CALIBRATION:
            data = _eeprom_accel_block()
        elif addr == REG_EXT_ID and self.extension_id:
            data = self.extension_id
        elif addr == REG_EXT_CALIBRATION and self.extension_id:
            data = nunchuk_calibration_block()
        else:
            self._queue.append(bytes([InputReport.READ_MEMORY_DATA, 0, 0, 0x08, (addr >> 8) & 0xFF, addr & 0xFF]) + bytes(16))
            return
        data = data[:size].ljust(size, b"\x00")
        for off in range(0, size, 16):
            chunk = data[off : off + 16]
            a = (addr + off) & 0xFFFF
            se = ((len(chunk) - 1) << 4) & 0xF0
            self._queue.append(bytes([InputReport.READ_MEMORY_DATA, 0, 0, se, a >> 8, a & 0xFF]) + chunk.ljust(16, b"\x00"))

    def _data_report(self, t: float) -> bytes:
        # Demo motion: stick circles, buttons cycle, gentle tilt.
        phase = int(t) % 6
        buttons = [0, BTN_A, BTN_B, BTN_UP | BTN_RIGHT, BTN_DOWN, BTN_LEFT][phase]
        tilt = math.sin(t * 0.7)
        accel = (512 + int(tilt * 80), 512, 616 - int(abs(tilt) * 20))
        sx = int(128 + 90 * math.cos(t * 1.5))
        sy = int(128 + 90 * math.sin(t * 1.5))
        c, z = phase in (1, 2), phase in (2, 3)
        ext = encode_nunchuk(sx, sy, (512, 512, 716), c, z) if self.extension_id == NUNCHUK_ID else b"\xff" * 6

        layout = REPORT_LAYOUTS.get(self.mode, REPORT_LAYOUTS[0x30])
        size = 21
        payload = bytearray(size)
        payload[0:2] = encode_buttons(buttons, accel)
        if layout.accel_offset is not None:
            payload[layout.accel_offset : layout.accel_offset + 3] = encode_accel(accel)
        if layout.ir_offset is not None:
            # Two dots of a sensor bar, slowly drifting.
            x1, x2 = int(400 + 150 * math.sin(t)), int(600 + 150 * math.sin(t))
            y = int(384 + 100 * math.cos(t))
            ir = bytes([x1 & 0xFF, y & 0xFF, ((y >> 8) << 6) | ((x1 >> 8) << 4) | ((y >> 8) << 2) | (x2 >> 8), x2 & 0xFF, y & 0xFF])
            ir += b"\xff" * 5
            payload[layout.ir_offset : layout.ir_offset + 10] = ir[: layout.ir_length].ljust(layout.ir_length, b"\xff")
        if layout.ext_offset is not None:
            payload[layout.ext_offset : layout.ext_offset + layout.ext_length] = ext[: layout.ext_length].ljust(layout.ext_length, b"\x00")
        return bytes([self.mode]) + bytes(payload[: max(2, self._payload_len(layout))])

    @staticmethod
    def _payload_len(layout) -> int:
        ends = [2]
        if layout.accel_offset is not None:
            ends.append(layout.accel_offset + 3)
        if layout.ir_offset is not None:
            ends.append(layout.ir_offset + layout.ir_length)
        if layout.ext_offset is not None:
            ends.append(layout.ext_offset + layout.ext_length)
        return max(ends)


SIMULATED_INFO = WiimoteHidInfo(
    path=b"sim://wiimote/1", product_id=0x0330, serial="SIM-00-00-01", product="Simulated RVL-CNT-01-TR"
)


def simulated_enumerate() -> list[WiimoteHidInfo]:
    return [SIMULATED_INFO]
