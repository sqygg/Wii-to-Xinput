"""Low-level HID I/O for a Wiimote paired through the Windows Bluetooth stack.

Reads go through hidapi (overlapped ReadFile under the hood). Writes use
hidapi's WriteFile path by default and fall back to ``HidD_SetOutputReport``,
which is what some Bluetooth stacks (and a few Wiimote clones) require.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from dataclasses import dataclass
from typing import Protocol

from .constants import INPUT_REPORT_SIZE, NINTENDO_VID, OUTPUT_REPORT_SIZE, WIIMOTE_PIDS

log = logging.getLogger(__name__)

try:  # pragma: no cover - availability depends on the host
    import hid  # provided by the "hidapi" package
except ImportError:  # pragma: no cover
    hid = None


class DeviceDisconnected(Exception):
    """The Wiimote is gone (powered off, out of range, unpaired)."""


@dataclass(frozen=True)
class WiimoteHidInfo:
    path: bytes
    product_id: int
    serial: str
    product: str

    @property
    def model(self) -> str:
        return "RVL-CNT-01-TR" if self.product_id == 0x0330 else "RVL-CNT-01"

    @property
    def key(self) -> str:
        """Stable identity used to avoid opening the same remote twice."""
        return self.serial or self.path.decode(errors="replace")


class Transport(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def read(self, timeout_ms: int) -> bytes | None: ...
    def write(self, report: bytes) -> None: ...


def enumerate_wiimotes() -> list[WiimoteHidInfo]:
    """List Wiimote HID collections currently known to Windows.

    Note: Windows keeps the HID node of a *paired* remote even while it is
    switched off, so presence here does not guarantee it is connected; the
    session handshake (status request) is the real connectivity test.
    """
    if hid is None:
        raise RuntimeError("hidapi is not installed. Run: pip install hidapi")
    found: dict[bytes, WiimoteHidInfo] = {}
    for pid in WIIMOTE_PIDS:
        for d in hid.enumerate(NINTENDO_VID, pid):
            path = d.get("path") or b""
            if not path or path in found:
                continue
            found[path] = WiimoteHidInfo(
                path=path,
                product_id=d.get("product_id", pid),
                serial=(d.get("serial_number") or "").strip(),
                product=(d.get("product_string") or "Nintendo RVL-CNT-01").strip(),
            )
    return list(found.values())


class _WinOutputReportWriter:  # pragma: no cover - Windows only
    """Synchronous ``HidD_SetOutputReport`` on a separate CreateFile handle."""

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    FILE_SHARE_READ = 0x01
    FILE_SHARE_WRITE = 0x02
    OPEN_EXISTING = 3
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    def __init__(self, path: bytes):
        from ctypes import wintypes

        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._hid = ctypes.WinDLL("hid", use_last_error=True)
        self._k32.CreateFileW.restype = wintypes.HANDLE
        self._k32.CreateFileW.argtypes = [
            wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
            wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
        ]
        self._k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._hid.HidD_SetOutputReport.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.ULONG]
        self._hid.HidD_SetOutputReport.restype = wintypes.BOOLEAN
        handle = self._k32.CreateFileW(
            path.decode(errors="replace"),
            self.GENERIC_READ | self.GENERIC_WRITE,
            self.FILE_SHARE_READ | self.FILE_SHARE_WRITE,
            None, self.OPEN_EXISTING, 0, None,
        )
        if not handle or handle == self.INVALID_HANDLE_VALUE:
            raise OSError(ctypes.get_last_error(), "CreateFileW failed for HID path")
        self._handle = handle

    def write(self, report: bytes) -> bool:
        buf = ctypes.create_string_buffer(bytes(report), len(report))
        return bool(self._hid.HidD_SetOutputReport(self._handle, buf, len(report)))

    def close(self) -> None:
        if self._handle:
            self._k32.CloseHandle(self._handle)
            self._handle = None


class HidTransport:
    def __init__(self, path: bytes, write_method: str = "auto"):
        self.path = path
        self.write_method = write_method
        self._dev = None
        self._fallback: _WinOutputReportWriter | None = None
        self._use_fallback = write_method == "set_output_report"

    def open(self) -> None:
        if hid is None:
            raise RuntimeError("hidapi is not installed. Run: pip install hidapi")
        dev = hid.device()
        try:
            dev.open_path(self.path)
        except (OSError, IOError) as exc:
            raise DeviceDisconnected(f"cannot open HID device: {exc}") from exc
        self._dev = dev
        if sys.platform == "win32" and self.write_method != "hidapi":
            try:
                self._fallback = _WinOutputReportWriter(self.path)
            except OSError as exc:
                log.debug("SetOutputReport fallback unavailable: %s", exc)

    def close(self) -> None:
        if self._fallback is not None:
            try:
                self._fallback.close()
            except OSError:
                pass
            self._fallback = None
        if self._dev is not None:
            try:
                self._dev.close()
            except Exception:  # noqa: BLE001 - closing a dead handle may raise anything
                pass
            self._dev = None

    def read(self, timeout_ms: int) -> bytes | None:
        if self._dev is None:
            raise DeviceDisconnected("transport is closed")
        try:
            data = self._dev.read(INPUT_REPORT_SIZE + 10, timeout_ms)
        except (OSError, IOError, ValueError) as exc:
            raise DeviceDisconnected(f"read failed: {exc}") from exc
        return bytes(data) if data else None

    def write(self, report: bytes) -> None:
        if self._dev is None:
            raise DeviceDisconnected("transport is closed")
        # Windows insists on full-length output reports.
        buf = bytes(report).ljust(OUTPUT_REPORT_SIZE, b"\x00")

        if not self._use_fallback:
            try:
                if self._dev.write(buf) >= 0:
                    return
            except (OSError, IOError, ValueError) as exc:
                log.debug("hidapi write failed: %s", exc)
            if self._fallback is None:
                raise DeviceDisconnected("write failed")
        if self._fallback is not None and self._fallback.write(buf):
            if not self._use_fallback:
                log.info("Switching %s to HidD_SetOutputReport writes", self.path)
                self._use_fallback = True
            return
        raise DeviceDisconnected("write failed (WriteFile and HidD_SetOutputReport)")
