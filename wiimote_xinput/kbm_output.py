"""Keyboard & mouse output through the Win32 ``SendInput`` API.

* Keys are sent as **scan codes** (``KEYEVENTF_SCANCODE``), which DirectInput
  and raw-input games read, not only as virtual keys.
* Relative mouse motion is integrated per Wiimote report with sub-pixel
  carry-over, so slow movements stay smooth instead of being rounded to zero.
* Several Wiimotes can share the keyboard: presses are reference counted, so
  a key held on one remote is not released when another remote lets go of it.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
import time
from collections import Counter, deque
from typing import Protocol

from .config import MouseConfig
from .constants import EXTENDED_VKS, KEY_TARGETS
from .mapper import KBM_IDLE, KbmState

log = logging.getLogger(__name__)

WHEEL_DELTA = 120
WHEEL_REPEAT_S = 0.12  # a held wheel button scrolls one notch per interval

MOUSE_BUTTON_FLAGS = {
    # name: (down flag, up flag, mouseData)
    "MOUSE_LEFT": (0x0002, 0x0004, 0),
    "MOUSE_RIGHT": (0x0008, 0x0010, 0),
    "MOUSE_MIDDLE": (0x0020, 0x0040, 0),
    "MOUSE_X1": (0x0080, 0x0100, 1),
    "MOUSE_X2": (0x0080, 0x0100, 2),
}
WHEEL_TARGETS = {"WHEEL_UP": 1, "WHEEL_DOWN": -1}


class InputBackend(Protocol):
    def key(self, name: str, down: bool) -> None: ...
    def mouse_button(self, name: str, down: bool) -> None: ...
    def mouse_move(self, dx: int, dy: int) -> None: ...  # +dy = down (screen coordinates)
    def mouse_absolute(self, x: float, y: float) -> None: ...  # 0..1 of the primary screen
    def wheel(self, notches: int) -> None: ...


class RecordingBackend:
    """Collects events instead of injecting them (tests, --dry-run, non-Windows)."""

    def __init__(self, max_events: int = 2000) -> None:
        self.events: deque[tuple] = deque(maxlen=max_events)
        self.cursor = [0, 0]

    def key(self, name: str, down: bool) -> None:
        self.events.append(("key", name, down))

    def mouse_button(self, name: str, down: bool) -> None:
        self.events.append(("button", name, down))

    def mouse_move(self, dx: int, dy: int) -> None:
        self.cursor[0] += dx
        self.cursor[1] += dy
        self.events.append(("move", dx, dy))

    def mouse_absolute(self, x: float, y: float) -> None:
        self.events.append(("abs", round(x, 4), round(y, 4)))

    def wheel(self, notches: int) -> None:
        self.events.append(("wheel", notches))


class SendInputBackend:  # pragma: no cover - Windows only
    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1
    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_SCANCODE = 0x0008
    MOUSEEVENTF_MOVE = 0x0001
    MOUSEEVENTF_WHEEL = 0x0800
    MOUSEEVENTF_ABSOLUTE = 0x8000

    def __init__(self) -> None:
        from ctypes import wintypes

        ULONG_PTR = ctypes.c_size_t

        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
            ]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

        class _U(ctypes.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(ctypes.Structure):
            _fields_ = [("type", wintypes.DWORD), ("u", _U)]

        self._INPUT = INPUT
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self._user32.SendInput.restype = wintypes.UINT
        self._user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        self._user32.MapVirtualKeyW.restype = wintypes.UINT
        self._warned = False

    def _send(self, inp) -> None:
        if self._user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(self._INPUT)) != 1 and not self._warned:
            self._warned = True
            log.warning(
                "SendInput was blocked (error %d). Input cannot reach windows running as administrator "
                "unless this app is also run as administrator.",
                ctypes.get_last_error(),
            )

    def key(self, name: str, down: bool) -> None:
        vk = KEY_TARGETS[name][0]
        scan = self._user32.MapVirtualKeyW(vk, 0)  # MAPVK_VK_TO_VSC
        inp = self._INPUT(type=self.INPUT_KEYBOARD)
        if scan:
            flags = self.KEYEVENTF_SCANCODE | (self.KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VKS else 0)
            inp.u.ki.wScan = scan
        else:  # media keys etc. have no scan code; send the virtual key
            flags = self.KEYEVENTF_EXTENDEDKEY if vk in EXTENDED_VKS else 0
            inp.u.ki.wVk = vk
        if not down:
            flags |= self.KEYEVENTF_KEYUP
        inp.u.ki.dwFlags = flags
        self._send(inp)

    def mouse_button(self, name: str, down: bool) -> None:
        down_flag, up_flag, data = MOUSE_BUTTON_FLAGS[name]
        inp = self._INPUT(type=self.INPUT_MOUSE)
        inp.u.mi.dwFlags = down_flag if down else up_flag
        inp.u.mi.mouseData = data
        self._send(inp)

    def mouse_move(self, dx: int, dy: int) -> None:
        inp = self._INPUT(type=self.INPUT_MOUSE)
        inp.u.mi.dx, inp.u.mi.dy = dx, dy
        inp.u.mi.dwFlags = self.MOUSEEVENTF_MOVE
        self._send(inp)

    def mouse_absolute(self, x: float, y: float) -> None:
        inp = self._INPUT(type=self.INPUT_MOUSE)
        inp.u.mi.dx = int(round(x * 65535))
        inp.u.mi.dy = int(round(y * 65535))
        inp.u.mi.dwFlags = self.MOUSEEVENTF_MOVE | self.MOUSEEVENTF_ABSOLUTE
        self._send(inp)

    def wheel(self, notches: int) -> None:
        inp = self._INPUT(type=self.INPUT_MOUSE)
        inp.u.mi.mouseData = (notches * WHEEL_DELTA) & 0xFFFFFFFF
        inp.u.mi.dwFlags = self.MOUSEEVENTF_WHEEL
        self._send(inp)


class _SharedPresses:
    """Reference-counts held keys/buttons across all Wiimotes."""

    def __init__(self, backend: InputBackend):
        self.backend = backend
        self._lock = threading.Lock()
        self._counts: Counter[str] = Counter()

    def press(self, name: str) -> None:
        with self._lock:
            self._counts[name] += 1
            if self._counts[name] == 1:
                self._emit(name, True)

    def release(self, name: str) -> None:
        with self._lock:
            if self._counts[name] <= 0:
                return
            self._counts[name] -= 1
            if self._counts[name] == 0:
                del self._counts[name]
                self._emit(name, False)

    def _emit(self, name: str, down: bool) -> None:
        try:
            if name in MOUSE_BUTTON_FLAGS:
                self.backend.mouse_button(name, down)
            elif name in KEY_TARGETS:
                self.backend.key(name, down)
        except Exception:  # noqa: BLE001 - input injection must never crash a session
            log.exception("Failed to send %s %s", name, "down" if down else "up")


class KeyboardMouseOutput:
    """Per-Wiimote translator from :class:`KbmState` to input events."""

    def __init__(self, shared: _SharedPresses):
        self._shared = shared
        self._held: frozenset[str] = frozenset()
        self._last_time: float | None = None
        self._carry = [0.0, 0.0]
        self._abs_smoothed: tuple[float, float] | None = None
        self._abs_sent: tuple[float, float] | None = None
        self._wheel_next = 0.0
        self.last: KbmState = KBM_IDLE

    @property
    def backend(self) -> InputBackend:
        return self._shared.backend

    def apply(self, kbm: KbmState, cfg: MouseConfig, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.last = kbm

        # Keys and mouse buttons (wheel "buttons" are handled separately)
        wanted = frozenset(k for k in kbm.keys if k not in WHEEL_TARGETS)
        for name in self._held - wanted:
            self._shared.release(name)
        for name in wanted - self._held:
            self._shared.press(name)
        self._held = wanted

        # Mouse wheel: one notch on press, then auto-repeat while held
        notches = sum(d for name, d in WHEEL_TARGETS.items() if name in kbm.keys)
        if notches and now >= self._wheel_next:
            self.backend.wheel(notches)
            self._wheel_next = now + WHEEL_REPEAT_S
        elif not notches:
            self._wheel_next = 0.0

        # Relative motion: integrate velocity over the time since the last report
        dt = 0.0 if self._last_time is None else min(0.05, now - self._last_time)
        self._last_time = now
        vx, vy = kbm.mouse_velocity
        if vx or vy:
            self._carry[0] += vx * cfg.speed * dt
            self._carry[1] -= vy * cfg.speed * dt  # stick up = screen up = negative dy
            dx, dy = int(self._carry[0]), int(self._carry[1])
            if dx or dy:
                self._carry[0] -= dx
                self._carry[1] -= dy
                self.backend.mouse_move(dx, dy)
        else:
            self._carry = [0.0, 0.0]

        # Absolute pointer: exponential smoothing against IR jitter
        if kbm.mouse_absolute is not None:
            target = ((kbm.mouse_absolute[0] + 1) / 2, (1 - kbm.mouse_absolute[1]) / 2)
            if self._abs_smoothed is None:
                self._abs_smoothed = target
            else:
                a = cfg.smoothing
                sx, sy = self._abs_smoothed
                self._abs_smoothed = (sx * a + target[0] * (1 - a), sy * a + target[1] * (1 - a))
            pos = (round(self._abs_smoothed[0], 4), round(self._abs_smoothed[1], 4))
            if pos != self._abs_sent:
                self.backend.mouse_absolute(*pos)
                self._abs_sent = pos
        else:
            self._abs_smoothed = None

    def release_all(self) -> None:
        """Let go of everything this Wiimote holds (disconnect, shutdown)."""
        for name in self._held:
            self._shared.release(name)
        self._held = frozenset()
        self._carry = [0.0, 0.0]
        self._abs_smoothed = None
        self._last_time = None
        self.last = KBM_IDLE


class KbmOutputFactory:
    """Creates per-Wiimote outputs that share one backend and press counter."""

    def __init__(self, backend: InputBackend):
        self._shared = _SharedPresses(backend)

    @property
    def backend(self) -> InputBackend:
        return self._shared.backend

    def __call__(self) -> KeyboardMouseOutput:
        return KeyboardMouseOutput(self._shared)


def create_kbm_factory(dry_run: bool = False) -> KbmOutputFactory:
    if dry_run or sys.platform != "win32":
        return KbmOutputFactory(RecordingBackend())
    return KbmOutputFactory(SendInputBackend())
