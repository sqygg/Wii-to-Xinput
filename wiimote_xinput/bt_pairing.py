"""Pair Wiimotes through the Win32 Bluetooth API (BluetoothApis.dll).

Why: the Windows 11 "Add device" dialog asks for a PIN that a Wiimote does
not have. The Wiimote's real PIN is binary: the host radio's MAC address
(when the red SYNC button is used) or the Wiimote's own MAC address (when
1+2 is used), as six raw bytes in little-endian order. The Settings UI can
not type those, but ``BluetoothAuthenticateDevice`` can.

Flow (repeated until a Wiimote is connected or the timeout expires):
1. Run an inquiry on every local radio.
2. For every device named ``Nintendo RVL-CNT-01*``:
   * stale pairing (remembered but not connected) -> remove it, it will be
     rediscovered on the next inquiry while the remote is in sync mode;
   * otherwise authenticate with the binary PIN (permanent pairing) and
     enable the HID service so Windows creates the HID device node.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

from .constants import BALANCE_BOARD_BT_NAME, WIIMOTE_BT_NAMES

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]

BLUETOOTH_MAX_NAME_SIZE = 248
BLUETOOTH_SERVICE_ENABLE = 0x01
ERROR_SUCCESS = 0
ERROR_NO_MORE_ITEMS = 259


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


# {00001124-0000-1000-8000-00805F9B34FB}
HID_SERVICE_GUID = GUID(0x00001124, 0x0000, 0x1000, (ctypes.c_ubyte * 8)(0x80, 0x00, 0x00, 0x80, 0x5F, 0x9B, 0x34, 0xFB))


class SYSTEMTIME(ctypes.Structure):
    _fields_ = [(n, wintypes.WORD) for n in (
        "wYear", "wMonth", "wDayOfWeek", "wDay", "wHour", "wMinute", "wSecond", "wMilliseconds")]


class BLUETOOTH_FIND_RADIO_PARAMS(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD)]


class BLUETOOTH_RADIO_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("address", ctypes.c_ulonglong),  # BLUETOOTH_ADDRESS union (8-byte aligned)
        ("szName", wintypes.WCHAR * BLUETOOTH_MAX_NAME_SIZE),
        ("ulClassofDevice", wintypes.ULONG),
        ("lmpSubversion", wintypes.USHORT),
        ("manufacturer", wintypes.USHORT),
    ]


class BLUETOOTH_DEVICE_SEARCH_PARAMS(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("fReturnAuthenticated", wintypes.BOOL),
        ("fReturnRemembered", wintypes.BOOL),
        ("fReturnUnknown", wintypes.BOOL),
        ("fReturnConnected", wintypes.BOOL),
        ("fIssueInquiry", wintypes.BOOL),
        ("cTimeoutMultiplier", ctypes.c_ubyte),  # units of 1.28 s
        ("hRadio", wintypes.HANDLE),
    ]


class BLUETOOTH_DEVICE_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("Address", ctypes.c_ulonglong),
        ("ulClassofDevice", wintypes.ULONG),
        ("fConnected", wintypes.BOOL),
        ("fRemembered", wintypes.BOOL),
        ("fAuthenticated", wintypes.BOOL),
        ("stLastSeen", SYSTEMTIME),
        ("stLastUsed", SYSTEMTIME),
        ("szName", wintypes.WCHAR * BLUETOOTH_MAX_NAME_SIZE),
    ]


def format_address(addr: int) -> str:
    return ":".join(f"{(addr >> (8 * i)) & 0xFF:02X}" for i in reversed(range(6)))


def address_pin(addr: int) -> list[int]:
    """The six address bytes, little-endian; each becomes one UTF-16 code unit.

    The PIN may contain zero bytes, so it is passed as a raw WCHAR[6] buffer
    rather than a NUL-terminated string."""
    return [(addr >> (8 * i)) & 0xFF for i in range(6)]


@dataclass(frozen=True)
class PairResult:
    name: str
    address: str
    authenticated: bool


class _BtApi:  # pragma: no cover - Windows only
    def __init__(self) -> None:
        dll = None
        for name in ("BluetoothApis.dll", "bthprops.cpl"):
            try:
                dll = ctypes.WinDLL(name, use_last_error=True)
                break
            except OSError:
                continue
        if dll is None:
            raise OSError("Bluetooth API not available (is a Bluetooth adapter installed?)")
        self.dll = dll
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        H = wintypes.HANDLE
        self._sig("BluetoothFindFirstRadio", H, [ctypes.POINTER(BLUETOOTH_FIND_RADIO_PARAMS), ctypes.POINTER(H)])
        self._sig("BluetoothFindNextRadio", wintypes.BOOL, [H, ctypes.POINTER(H)])
        self._sig("BluetoothFindRadioClose", wintypes.BOOL, [H])
        self._sig("BluetoothGetRadioInfo", wintypes.DWORD, [H, ctypes.POINTER(BLUETOOTH_RADIO_INFO)])
        self._sig("BluetoothFindFirstDevice", H, [ctypes.POINTER(BLUETOOTH_DEVICE_SEARCH_PARAMS), ctypes.POINTER(BLUETOOTH_DEVICE_INFO)])
        self._sig("BluetoothFindNextDevice", wintypes.BOOL, [H, ctypes.POINTER(BLUETOOTH_DEVICE_INFO)])
        self._sig("BluetoothFindDeviceClose", wintypes.BOOL, [H])
        self._sig("BluetoothRemoveDevice", wintypes.DWORD, [ctypes.POINTER(ctypes.c_ulonglong)])
        self._sig("BluetoothAuthenticateDevice", wintypes.DWORD,
                  [wintypes.HWND, H, ctypes.POINTER(BLUETOOTH_DEVICE_INFO), ctypes.POINTER(wintypes.WCHAR), wintypes.ULONG])
        self._sig("BluetoothSetServiceState", wintypes.DWORD,
                  [H, ctypes.POINTER(BLUETOOTH_DEVICE_INFO), ctypes.POINTER(GUID), wintypes.DWORD])
        self.k32.CloseHandle.argtypes = [H]

    def _sig(self, name: str, restype, argtypes) -> None:
        fn = getattr(self.dll, name)
        fn.restype = restype
        fn.argtypes = argtypes

    def radios(self) -> list[tuple[int, int]]:
        """Return [(handle, radio_address)]; caller closes the handles."""
        params = BLUETOOTH_FIND_RADIO_PARAMS(ctypes.sizeof(BLUETOOTH_FIND_RADIO_PARAMS))
        h = wintypes.HANDLE()
        find = self.dll.BluetoothFindFirstRadio(ctypes.byref(params), ctypes.byref(h))
        out: list[tuple[int, int]] = []
        if not find:
            return out
        try:
            while True:
                info = BLUETOOTH_RADIO_INFO()
                info.dwSize = ctypes.sizeof(BLUETOOTH_RADIO_INFO)
                if self.dll.BluetoothGetRadioInfo(h, ctypes.byref(info)) == ERROR_SUCCESS:
                    out.append((h.value, info.address))
                else:
                    self.k32.CloseHandle(h)
                h = wintypes.HANDLE()
                if not self.dll.BluetoothFindNextRadio(find, ctypes.byref(h)):
                    break
        finally:
            self.dll.BluetoothFindRadioClose(find)
        return out

    def devices(self, radio: int, inquiry: bool, timeout_mult: int = 2) -> list[BLUETOOTH_DEVICE_INFO]:
        params = BLUETOOTH_DEVICE_SEARCH_PARAMS(
            ctypes.sizeof(BLUETOOTH_DEVICE_SEARCH_PARAMS), True, True, True, True, inquiry, timeout_mult, radio
        )
        info = BLUETOOTH_DEVICE_INFO()
        info.dwSize = ctypes.sizeof(BLUETOOTH_DEVICE_INFO)
        find = self.dll.BluetoothFindFirstDevice(ctypes.byref(params), ctypes.byref(info))
        out: list[BLUETOOTH_DEVICE_INFO] = []
        if not find:
            return out
        try:
            while True:
                out.append(info)
                info = BLUETOOTH_DEVICE_INFO()
                info.dwSize = ctypes.sizeof(BLUETOOTH_DEVICE_INFO)
                if not self.dll.BluetoothFindNextDevice(find, ctypes.byref(info)):
                    break
        finally:
            self.dll.BluetoothFindDeviceClose(find)
        return out


def is_supported() -> bool:
    return sys.platform == "win32"


def list_wiimotes() -> list[PairResult]:  # pragma: no cover - Windows only
    """Return Wiimotes Windows already knows about (no inquiry)."""
    api = _BtApi()
    found = []
    for radio, _addr in api.radios():
        try:
            for dev in api.devices(radio, inquiry=False):
                if dev.szName in WIIMOTE_BT_NAMES:
                    found.append(PairResult(dev.szName, format_address(dev.Address), bool(dev.fAuthenticated)))
        finally:
            api.k32.CloseHandle(radio)
    return found


def pair_wiimotes(
    timeout_s: float = 30.0,
    progress: ProgressCallback | None = None,
    pin_mode: str = "sync",
    remove_stale: bool = True,
    should_stop: Callable[[], bool] | None = None,
) -> list[PairResult]:  # pragma: no cover - Windows only
    """Discover and pair Wiimotes. Press the red SYNC button (``pin_mode='sync'``,
    permanent pairing) or hold 1+2 (``pin_mode='buttons'``) before calling."""
    if not is_supported():
        raise RuntimeError("Wiimote pairing helper requires Windows")

    def say(msg: str) -> None:
        log.info(msg)
        if progress:
            progress(msg)

    api = _BtApi()
    paired: dict[str, PairResult] = {}
    deadline = time.monotonic() + timeout_s
    say("Searching for Wiimotes… press the red SYNC button now.")
    while time.monotonic() < deadline and not (should_stop and should_stop()):
        radios = api.radios()
        if not radios:
            raise RuntimeError("No Bluetooth radio found. Is Bluetooth turned on?")
        for radio, radio_addr in radios:
            try:
                for dev in api.devices(radio, inquiry=True):
                    name = dev.szName
                    if name == BALANCE_BOARD_BT_NAME or name not in WIIMOTE_BT_NAMES:
                        continue
                    addr = format_address(dev.Address)
                    if dev.fConnected:
                        if addr not in paired:
                            paired[addr] = PairResult(name, addr, bool(dev.fAuthenticated))
                            say(f"{name} [{addr}] is connected.")
                        continue
                    if dev.fRemembered and remove_stale:
                        say(f"Removing stale pairing for {addr}…")
                        api.dll.BluetoothRemoveDevice(ctypes.byref(ctypes.c_ulonglong(dev.Address)))
                        continue
                    say(f"Found {name} [{addr}], pairing…")
                    authenticated = False
                    if pin_mode in ("sync", "buttons"):
                        pin_src = radio_addr if pin_mode == "sync" else dev.Address
                        pin = (wintypes.WCHAR * 6)(*map(chr, address_pin(pin_src)))
                        res = api.dll.BluetoothAuthenticateDevice(None, radio, ctypes.byref(dev), pin, 6)
                        authenticated = res == ERROR_SUCCESS
                        if not authenticated:
                            say(f"Authentication returned {res}; continuing without a bond.")
                    res = api.dll.BluetoothSetServiceState(
                        radio, ctypes.byref(dev), ctypes.byref(HID_SERVICE_GUID), BLUETOOTH_SERVICE_ENABLE
                    )
                    if res == ERROR_SUCCESS:
                        paired[addr] = PairResult(name, addr, authenticated)
                        say(f"Paired {name} [{addr}]{' (permanent)' if authenticated else ''}.")
                    else:
                        say(f"Enabling the HID service failed with error {res}; retrying…")
            finally:
                api.k32.CloseHandle(radio)
        if paired:
            break
    if not paired:
        say("No Wiimote found. Make sure the remote's LEDs are blinking while searching.")
    return list(paired.values())
