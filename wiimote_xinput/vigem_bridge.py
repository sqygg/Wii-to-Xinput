"""Virtual Xbox 360 controller output through the ViGEmBus driver (vgamepad)."""

from __future__ import annotations

import importlib.util
import logging
import os
import platform
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .mapper import NEUTRAL, XInputState

log = logging.getLogger(__name__)

VIGEMBUS_DOWNLOAD_URL = "https://github.com/nefarius/ViGEmBus/releases/latest"

# (large_motor, small_motor) in 0..255
RumbleCallback = Callable[[int, int], None]


class VirtualPad(Protocol):
    def submit(self, state: XInputState) -> None: ...
    def neutral(self) -> None: ...
    def close(self) -> None: ...
    def set_rumble_callback(self, cb: RumbleCallback | None) -> None: ...


class ViGEmX360Pad:
    """One virtual Xbox 360 controller plugged into ViGEmBus."""

    def __init__(self) -> None:
        import vgamepad as vg  # imported lazily: fails if the driver is missing

        self._vg = vg
        self._pad = vg.VX360Gamepad()
        self._lock = threading.Lock()
        self._rumble_cb: RumbleCallback | None = None
        self._last: XInputState | None = None
        self._pad.register_notification(callback_function=self._on_notification)
        log.info("Virtual Xbox 360 controller plugged in")

    def _on_notification(self, client, target, large_motor, small_motor, led_number, user_data):
        cb = self._rumble_cb
        if cb is not None:
            try:
                cb(int(large_motor), int(small_motor))
            except Exception:  # noqa: BLE001 - never let exceptions cross into C
                log.exception("Rumble callback failed")

    def set_rumble_callback(self, cb: RumbleCallback | None) -> None:
        self._rumble_cb = cb

    def submit(self, state: XInputState) -> None:
        if state == self._last:
            return
        with self._lock:
            if self._pad is None:
                return
            r = self._pad.report
            r.wButtons = state.buttons
            r.bLeftTrigger = state.lt
            r.bRightTrigger = state.rt
            r.sThumbLX = state.lx
            r.sThumbLY = state.ly
            r.sThumbRX = state.rx
            r.sThumbRY = state.ry
            self._pad.update()
            self._last = state

    def neutral(self) -> None:
        self.submit(NEUTRAL)

    def close(self) -> None:
        with self._lock:
            pad, self._pad = self._pad, None
        if pad is None:
            return
        self._rumble_cb = None
        try:
            pad.reset()
            pad.update()
            pad.unregister_notification()
        except Exception:  # noqa: BLE001
            log.debug("Error while resetting virtual pad", exc_info=True)
        del pad  # vgamepad unplugs the target in __del__
        log.info("Virtual Xbox 360 controller unplugged")


class DryRunPad:
    """Stand-in used with --dry-run and in tests: records the last state."""

    def __init__(self) -> None:
        self.last: XInputState = NEUTRAL
        self.submits = 0
        self.closed = False
        self._rumble_cb: RumbleCallback | None = None

    def submit(self, state: XInputState) -> None:
        self.last = state
        self.submits += 1

    def neutral(self) -> None:
        self.submit(NEUTRAL)

    def close(self) -> None:
        self.closed = True

    def set_rumble_callback(self, cb: RumbleCallback | None) -> None:
        self._rumble_cb = cb

    def fake_rumble(self, large: int, small: int) -> None:
        if self._rumble_cb:
            self._rumble_cb(large, small)


# --------------------------------------------------------------------------
# Driver detection / installation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DriverStatus:
    service_installed: bool
    client_ok: bool
    message: str

    @property
    def ok(self) -> bool:
        return self.client_ok


def _service_registered() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Services\ViGEmBus"):
            return True
    except OSError:
        return False


def check_vigembus() -> DriverStatus:
    """Verify that ViGEmBus is installed and that vgamepad can talk to it."""
    if sys.platform != "win32":
        return DriverStatus(False, False, "ViGEmBus is only available on Windows (use --dry-run here).")
    service = _service_registered()
    if importlib.util.find_spec("vgamepad") is None:
        return DriverStatus(service, False, "Python package 'vgamepad' is not installed (pip install vgamepad).")
    try:
        import vgamepad  # noqa: F401  (connects to the bus on import)
    except Exception as exc:  # noqa: BLE001 - vgamepad raises plain Exception/AssertionError
        if not service:
            return DriverStatus(False, False, "ViGEmBus driver is not installed.")
        return DriverStatus(True, False, f"ViGEmBus is installed but not reachable ({exc}). Try rebooting.")
    return DriverStatus(True, True, "ViGEmBus driver detected.")


def find_bundled_installer() -> Path | None:
    """vgamepad ships the official ViGEmBus MSI; locate it without importing
    vgamepad (importing would fail when the driver is absent)."""
    spec = importlib.util.find_spec("vgamepad")
    if spec is None or not spec.submodule_search_locations:
        return None
    root = Path(next(iter(spec.submodule_search_locations)))
    arch = "x64" if platform.machine().endswith("64") else "x86"
    for candidate in sorted(root.rglob("ViGEmBus*.msi")) + sorted(root.rglob("ViGEmBus*.exe")):
        if arch in candidate.parts or arch in candidate.name:
            return candidate
    return next(iter(root.rglob("ViGEmBus*.msi")), None)


def launch_installer(installer: Path) -> None:
    """Start the ViGEmBus installer (elevation prompt is handled by Windows)."""
    if sys.platform != "win32":
        raise RuntimeError("The ViGEmBus installer only runs on Windows")
    if installer.suffix.lower() == ".msi":
        subprocess.Popen(["msiexec", "/i", str(installer)], close_fds=True)
    else:
        os.startfile(str(installer))  # type: ignore[attr-defined]


def create_pad(dry_run: bool = False) -> VirtualPad:
    return DryRunPad() if dry_run else ViGEmX360Pad()
