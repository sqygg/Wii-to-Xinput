"""Entry point: ``python -m wiimote_xinput [options]``."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import __version__
from .config import ConfigStore, config_dir

log = logging.getLogger("wiimote_xinput")


def setup_logging(verbose: bool) -> Path:
    log_dir = config_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "wiimote_xinput.log"
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s")
    try:
        fh = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
        fh.setFormatter(fmt)
        root.addHandler(fh)
    except OSError:
        pass
    if sys.stderr is not None:  # pythonw.exe has no console
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S"))
        root.addHandler(ch)
    return log_file


def acquire_single_instance():
    """Two instances would fight over the same Wiimote; refuse to start twice."""
    if sys.platform != "win32":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, "Local\\WiimoteXInputSingleton")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        return None
    return handle


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="wiimote_xinput", description="Use a Wii Remote as an Xbox 360 (XInput) controller.")
    p.add_argument("--headless", action="store_true", help="run without the GUI (console only)")
    p.add_argument("--minimized", action="store_true", help="start minimized to the system tray")
    p.add_argument("--dry-run", action="store_true", help="decode input but do not create virtual controllers")
    p.add_argument("--simulate", action="store_true", help="use a simulated Wiimote + Nunchuk instead of hardware")
    p.add_argument("--pair", action="store_true", help="run the Bluetooth pairing helper and exit")
    p.add_argument("--pair-mode", choices=("sync", "buttons"), default="sync", help="SYNC button (permanent) or 1+2")
    p.add_argument("--list", action="store_true", help="list Wiimote HID devices and exit")
    p.add_argument("--check-driver", action="store_true", help="check the ViGEmBus driver and exit")
    p.add_argument("--config", type=Path, help="path to config.json")
    p.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    log_file = setup_logging(args.verbose)
    log.info("Wiimote -> XInput %s (log: %s)", __version__, log_file)

    if args.pair:
        from .bt_pairing import pair_wiimotes

        try:
            results = pair_wiimotes(progress=print, pin_mode=args.pair_mode)
        except Exception as exc:  # noqa: BLE001
            print(f"Pairing failed: {exc}")
            return 1
        return 0 if results else 2

    if args.list:
        from .hid_transport import enumerate_wiimotes

        devices = enumerate_wiimotes()
        if not devices:
            print("No Wiimote HID devices found. Pair the Wiimote first.")
        for d in devices:
            print(f"{d.model:15} serial={d.serial or '-':20} path={d.path.decode(errors='replace')}")
        return 0

    from .vigem_bridge import check_vigembus, create_pad

    if args.check_driver:
        status = check_vigembus()
        print(status.message)
        return 0 if status.ok else 1

    if acquire_single_instance() is None:
        msg = "Wiimote -> XInput is already running (check the system tray)."
        log.error(msg)
        if not args.headless:
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox

                app = QApplication(sys.argv)  # noqa: F841 - required before showing a dialog
                QMessageBox.information(None, "Wiimote → XInput", msg)
            except ImportError:
                pass
        return 1

    store = ConfigStore(args.config).load()
    dry_run = args.dry_run
    from .bluetooth_core import DeviceManager

    if args.simulate:
        from .simulator import SimulatedTransport, simulated_enumerate

        manager = DeviceManager(
            store,
            pad_factory=lambda: create_pad(dry_run),
            enumerate_fn=simulated_enumerate,
            transport_factory=lambda info: SimulatedTransport(),
        )
    else:
        manager = DeviceManager(store, pad_factory=lambda: create_pad(dry_run))

    if args.headless:
        return run_headless(manager, dry_run, check_vigembus)

    try:
        from .ui import run_gui
    except ImportError as exc:
        log.error("GUI unavailable (%s). Install PySide6 or use --headless.", exc)
        return 1
    return run_gui(
        store,
        manager,
        check_vigembus,
        dry_run=dry_run,
        start_minimized=args.minimized or store.settings.start_minimized,
    )


def run_headless(manager, dry_run: bool, driver_check) -> int:
    if not dry_run:
        status = driver_check()
        if not status.ok:
            log.error("%s Install ViGEmBus or run with --dry-run.", status.message)
            return 1
    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, lambda *_: stop.set())
    manager.add_listener(lambda ev, info: log.info("[%s] P%d %s %s battery=%s ext=%s",
                                                    ev, info.slot, info.model, info.status.value,
                                                    info.battery_percent, info.extension.label))
    manager.start()
    log.info("Running headless. Press Ctrl+C to quit.")
    try:
        while not stop.wait(0.5):
            pass
    finally:
        manager.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
