"""Windows integration helpers: auto-start, settings shortcuts."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import webbrowser

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "WiimoteXInput"


def launch_command(minimized: bool = True) -> str:
    """Command line that starts this app (works for PyInstaller builds too)."""
    suffix = " --minimized" if minimized else ""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"{suffix}'
    exe = sys.executable
    pythonw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if os.path.exists(pythonw):
        exe = pythonw  # no console window
    return f'"{exe}" -m wiimote_xinput{suffix}'


def set_autostart(enabled: bool) -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, launch_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError as exc:
        log.error("Could not update auto-start entry: %s", exc)
        return False


def open_bluetooth_settings() -> None:
    if sys.platform == "win32":
        os.startfile("ms-settings:bluetooth")  # type: ignore[attr-defined]
    else:
        log.info("Bluetooth settings shortcut is only available on Windows")


def open_devices_and_printers() -> None:
    if sys.platform == "win32":
        subprocess.Popen(["control", "printers"])


def open_url(url: str) -> None:
    webbrowser.open(url)
