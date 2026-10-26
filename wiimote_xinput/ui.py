"""PySide6 desktop UI: dashboard, mapping editor, settings, log and tray icon."""

from __future__ import annotations

import logging
import sys
import threading
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QActionGroup, QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__, bt_pairing, platform_utils
from .bluetooth_core import ConnectionStatus, DeviceManager, SessionInfo
from .config import ConfigStore, Profile
from .constants import (
    BUTTON_SOURCES,
    KBM_TARGET_LABELS,
    KEY_STICK_LAYOUTS,
    MOUSE_MODES,
    ORIENTATIONS,
    SOURCE_LABELS,
    STICK_SOURCES,
    TRIGGER_SOURCES,
    XINPUT_TARGET_LABELS,
    XINPUT_TARGETS,
)
from .vigem_bridge import VIGEMBUS_DOWNLOAD_URL, DriverStatus, find_bundled_installer, launch_installer
from .widgets import BAD, MUTED, OK, WARN, Banner, ControllerCard, LabeledSlider, empty_state_widget, make_app_icon

log = logging.getLogger(__name__)

UI_REFRESH_MS = 33  # ~30 fps live view; input latency is unaffected by this


class _Signals(QObject):
    session_event = Signal(str, object)
    log_line = Signal(str)
    pair_progress = Signal(str)
    pair_done = Signal(object)


class QtLogHandler(logging.Handler):
    def __init__(self, signal: Signal):
        super().__init__(logging.INFO)
        self._signal = signal
        self.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._signal.emit(self.format(record))
        except RuntimeError:  # Qt object already destroyed during shutdown
            pass


# --------------------------------------------------------------------------
# Pairing dialog
# --------------------------------------------------------------------------


class PairDialog(QDialog):
    def __init__(self, on_paired: Callable[[], None], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pair a Wiimote")
        self.setMinimumWidth(520)
        self._on_paired = on_paired
        self._cancel = False
        self._thread: threading.Thread | None = None
        self._sig = _Signals()
        self._sig.pair_progress.connect(self._append)
        self._sig.pair_done.connect(self._done)

        intro = QLabel(
            "<ol>"
            "<li>Make sure Bluetooth is on.</li>"
            "<li>Open the battery cover and press the small red <b>SYNC</b> button"
            " (recommended — pairs permanently, so later any button reconnects),"
            " or hold <b>1 + 2</b> (temporary, must be repeated each time).</li>"
            "<li>While the four LEDs blink, click <b>Start</b>.</li>"
            "</ol>"
        )
        intro.setWordWrap(True)
        self.rb_sync = QRadioButton("I pressed the red SYNC button (permanent pairing)")
        self.rb_buttons = QRadioButton("I am holding 1 + 2 (temporary connection)")
        self.rb_sync.setChecked(True)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.start_btn = QPushButton("Start")
        self.start_btn.clicked.connect(self._start)
        settings_btn = QPushButton("Open Windows Bluetooth settings")
        settings_btn.clicked.connect(platform_utils.open_bluetooth_settings)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(intro)
        lay.addWidget(self.rb_sync)
        lay.addWidget(self.rb_buttons)
        row = QHBoxLayout()
        row.addWidget(self.start_btn)
        row.addWidget(settings_btn)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addWidget(self.log, 1)
        lay.addWidget(buttons)

        if not bt_pairing.is_supported():
            self.start_btn.setEnabled(False)
            self._append("The pairing helper requires Windows.")

    def _append(self, text: str) -> None:
        self.log.appendPlainText(text)

    def _start(self) -> None:
        self.start_btn.setEnabled(False)
        self._cancel = False
        mode = "sync" if self.rb_sync.isChecked() else "buttons"

        def work() -> None:
            try:
                result = bt_pairing.pair_wiimotes(
                    timeout_s=30.0,
                    progress=self._sig.pair_progress.emit,
                    pin_mode=mode,
                    should_stop=lambda: self._cancel,
                )
                self._sig.pair_done.emit(result)
            except Exception as exc:  # noqa: BLE001 - surface any failure to the user
                self._sig.pair_done.emit(exc)

        self._thread = threading.Thread(target=work, name="WiimotePairing", daemon=True)
        self._thread.start()

    def _done(self, result) -> None:
        self.start_btn.setEnabled(True)
        if isinstance(result, Exception):
            self._append(f"Error: {result}")
            return
        if result:
            self._append("Done. The Wiimote should appear on the dashboard within a few seconds.")
            self._on_paired()

    def reject(self) -> None:
        self._cancel = True
        super().reject()


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------


class MainWindow(QMainWindow):
    def __init__(
        self,
        store: ConfigStore,
        manager: DeviceManager,
        driver_check: Callable[[], DriverStatus],
        dry_run: bool = False,
    ):
        super().__init__()
        self.store = store
        self.manager = manager
        self._driver_check = driver_check
        self._dry_run = dry_run
        self._manager_started = False
        self._quitting = False
        self._tray_hint_shown = False
        self._cards: dict[str, ControllerCard] = {}
        self._low_battery_warned: set[str] = set()
        self._profile: Profile = store.active_profile.copy()
        self._sig = _Signals()
        self._tray_state: tuple | None = None

        self.setWindowTitle("Wiimote → XInput")
        self.setWindowIcon(make_app_icon())
        self.resize(820, 680)

        self._save_timer = QTimer(self, singleShot=True, interval=400)
        self._save_timer.timeout.connect(self.store.save)

        self._build_menu()
        central = QWidget()
        root = QVBoxLayout(central)
        root.addLayout(self._build_profile_bar())
        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_dashboard(), "Dashboard")
        self.tabs.addTab(self._build_mapping_tab(), "Mapping")
        self.tabs.addTab(self._build_settings_tab(), "Settings")
        self.tabs.addTab(self._build_log_tab(), "Log")
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        self._build_tray()
        self._load_profile_into_widgets()
        self._load_settings_into_widgets()

        self._log_handler = QtLogHandler(self._sig.log_line)
        logging.getLogger().addHandler(self._log_handler)
        self._sig.log_line.connect(self.log_view.appendPlainText)
        self._sig.session_event.connect(self._on_session_event)
        self.manager.add_listener(lambda ev, info: self._sig.session_event.emit(ev, info))

        self._timer = QTimer(self, interval=UI_REFRESH_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._check_driver()

    # ------------------------------------------------------------ building

    def _build_menu(self) -> None:
        m_file = self.menuBar().addMenu("&File")
        act = m_file.addAction("Open configuration folder")
        act.triggered.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.store.path.parent))))
        m_file.addSeparator()
        m_file.addAction("Quit", self.quit_app)
        m_help = self.menuBar().addMenu("&Help")
        m_help.addAction("ViGEmBus driver download page", lambda: platform_utils.open_url(VIGEMBUS_DOWNLOAD_URL))
        m_help.addAction("Windows Bluetooth settings", platform_utils.open_bluetooth_settings)
        m_help.addSeparator()
        m_help.addAction("About", self._about)

    def _build_profile_bar(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.addWidget(QLabel("Active profile:"))
        self.profile_combo = QComboBox()
        self.profile_combo.setMinimumWidth(240)
        self.profile_combo.currentTextChanged.connect(self._on_profile_selected)
        row.addWidget(self.profile_combo)
        row.addStretch(1)
        pair = QPushButton("Pair Wiimote…")
        pair.clicked.connect(self._open_pair_dialog)
        rescan = QPushButton("Rescan")
        rescan.setToolTip("Look for newly connected Wiimotes now")
        rescan.clicked.connect(self.manager.rescan)
        row.addWidget(pair)
        row.addWidget(rescan)
        self._refresh_profile_combo()
        return row

    def _build_dashboard(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        self.driver_banner = Banner()
        self.install_btn = QPushButton("Install ViGEmBus")
        self.install_btn.clicked.connect(self._install_driver)
        self.recheck_btn = QPushButton("Re-check")
        self.recheck_btn.clicked.connect(self._check_driver)
        self.driver_banner.add_widget(self.install_btn)
        self.driver_banner.add_widget(self.recheck_btn)
        lay.addWidget(self.driver_banner)

        self.cards_host = QWidget()
        self.cards_layout = QVBoxLayout(self.cards_host)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.empty_widget = empty_state_widget()
        self.cards_layout.addWidget(self.empty_widget)
        self.cards_layout.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(self.cards_host)
        lay.addWidget(scroll, 1)
        return page

    def _build_mapping_tab(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)

        bar = QHBoxLayout()
        for text, slot in (
            ("New", self._profile_new),
            ("Duplicate", self._profile_duplicate),
            ("Rename", self._profile_rename),
            ("Delete", self._profile_delete),
            ("Import…", self._profile_import),
            ("Export…", self._profile_export),
            ("Reset to defaults", self._profile_reset),
        ):
            b = QPushButton(text)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        outer.addLayout(bar)

        content = QWidget()
        grid = QGridLayout(content)
        self._button_combos: dict[str, QComboBox] = {}

        self._key_combos: dict[str, QComboBox] = {}

        def button_group(title: str, prefix: str) -> QGroupBox:
            box = QGroupBox(title)
            table = QGridLayout(box)
            for col, text in ((1, "Xbox controller"), (2, "Keyboard / mouse")):
                head = QLabel(f"<b>{text}</b>")
                table.addWidget(head, 0, col)
            row = 1
            for source in BUTTON_SOURCES:
                if not source.startswith(prefix):
                    continue
                combo = QComboBox()
                for key in XINPUT_TARGETS:
                    combo.addItem(XINPUT_TARGET_LABELS[key], key)
                combo.currentIndexChanged.connect(lambda _i, s=source, c=combo: self._set_button(s, c.currentData()))
                self._button_combos[source] = combo
                kcombo = QComboBox()
                kcombo.setMaxVisibleItems(20)
                for key, label in KBM_TARGET_LABELS.items():
                    kcombo.addItem(label, key)
                kcombo.currentIndexChanged.connect(lambda _i, s=source, c=kcombo: self._set_key(s, c.currentData()))
                self._key_combos[source] = kcombo
                table.addWidget(QLabel(SOURCE_LABELS[source]), row, 0)
                table.addWidget(combo, row, 1)
                table.addWidget(kcombo, row, 2)
                row += 1
            table.setColumnStretch(1, 1)
            table.setColumnStretch(2, 1)
            return box

        general = QGroupBox("General")
        gform = QFormLayout(general)
        self.orientation_combo = QComboBox()
        for key, label in ORIENTATIONS.items():
            self.orientation_combo.addItem(label, key)
        self.orientation_combo.currentIndexChanged.connect(
            lambda _i: self._set_profile_attr("orientation", self.orientation_combo.currentData())
        )
        gform.addRow("Wiimote orientation", self.orientation_combo)

        analog = QGroupBox("Analog inputs")
        aform = QFormLayout(analog)
        self.stick_combos: dict[str, QComboBox] = {}
        for which, label in (("left_stick", "Left thumbstick"), ("right_stick", "Right thumbstick")):
            combo = QComboBox()
            for key, text in STICK_SOURCES.items():
                combo.addItem(text, key)
            combo.currentIndexChanged.connect(
                lambda _i, w=which, c=combo: self._set_sub(w, "source", c.currentData())
            )
            self.stick_combos[which] = combo
            aform.addRow(label, combo)
        self.trigger_combos: dict[str, QComboBox] = {}
        for which, label in (("left_trigger", "Left trigger (analog)"), ("right_trigger", "Right trigger (analog)")):
            combo = QComboBox()
            for key, text in TRIGGER_SOURCES.items():
                combo.addItem(text, key)
            combo.currentIndexChanged.connect(
                lambda _i, w=which, c=combo: self._set_sub(w, "source", c.currentData())
            )
            self.trigger_combos[which] = combo
            aform.addRow(label, combo)
        self.classic_fallback = QCheckBox("Classic Controller left stick always drives the left thumbstick")
        self.classic_fallback.toggled.connect(lambda v: self._set_profile_attr("classic_left_stick_fallback", v))
        aform.addRow(self.classic_fallback)

        kbm = QGroupBox("Keyboard && mouse")
        kform = QFormLayout(kbm)
        self.xinput_enabled = QCheckBox("Also drive the virtual Xbox controller")
        self.xinput_enabled.setToolTip(
            "Turn off for pure keyboard/mouse profiles so games do not see controller input at the same time"
        )
        self.xinput_enabled.toggled.connect(lambda v: self._set_profile_attr("xinput_enabled", v))
        kform.addRow(self.xinput_enabled)
        self.mouse_source = QComboBox()
        for key, text in STICK_SOURCES.items():
            self.mouse_source.addItem(text, key)
        self.mouse_source.currentIndexChanged.connect(
            lambda _i: self._set_sub("mouse", "source", self.mouse_source.currentData())
        )
        kform.addRow("Mouse pointer source", self.mouse_source)
        self.mouse_mode = QComboBox()
        for key, text in MOUSE_MODES.items():
            self.mouse_mode.addItem(text, key)
        self.mouse_mode.currentIndexChanged.connect(
            lambda _i: self._set_sub("mouse", "mode", self.mouse_mode.currentData())
        )
        kform.addRow("Mouse mode", self.mouse_mode)
        self.key_stick_source = QComboBox()
        for key, text in STICK_SOURCES.items():
            self.key_stick_source.addItem(text, key)
        self.key_stick_source.currentIndexChanged.connect(
            lambda _i: self._set_sub("key_stick", "source", self.key_stick_source.currentData())
        )
        kform.addRow("Movement keys source", self.key_stick_source)
        self.key_stick_layout = QComboBox()
        for key, text in KEY_STICK_LAYOUTS.items():
            self.key_stick_layout.addItem(text, key)
        self.key_stick_layout.currentIndexChanged.connect(
            lambda _i: self._set_sub("key_stick", "layout", self.key_stick_layout.currentData())
        )
        kform.addRow("Movement keys", self.key_stick_layout)

        grid.addWidget(general, 0, 0)
        grid.addWidget(analog, 1, 0)
        grid.addWidget(kbm, 0, 1, 2, 1)
        grid.addWidget(button_group("Wii Remote buttons", "wiimote."), 2, 0)
        grid.addWidget(button_group("Nunchuk buttons", "nunchuk."), 3, 0)
        grid.addWidget(button_group("Classic Controller buttons", "classic."), 2, 1, 2, 1)
        grid.setRowStretch(4, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        hint = QLabel("Changes apply instantly to connected controllers and are saved automatically.")
        hint.setStyleSheet(f"color: {MUTED.name()}")
        outer.addWidget(hint)
        return page

    def _build_settings_tab(self) -> QWidget:
        content = QWidget()
        grid = QGridLayout(content)
        self._stick_widgets: dict[str, dict[str, QWidget]] = {}

        for col, (which, title) in enumerate((("left_stick", "Left thumbstick"), ("right_stick", "Right thumbstick"))):
            box = QGroupBox(title)
            form = QFormLayout(box)
            widgets: dict[str, QWidget] = {
                "deadzone": LabeledSlider(0.0, 0.5, 0.01, "{:.0%}"),
                "saturation": LabeledSlider(0.5, 1.0, 0.01, "{:.0%}"),
                "curve": LabeledSlider(0.5, 3.0, 0.05, "{:.2f}"),
                "tilt_range_deg": LabeledSlider(10, 90, 1, "{:.0f}°"),
                "invert_x": QCheckBox("Invert X axis"),
                "invert_y": QCheckBox("Invert Y axis"),
            }
            labels = {
                "deadzone": "Deadzone",
                "saturation": "Outer saturation",
                "curve": "Response curve",
                "tilt_range_deg": "Tilt range (tilt sources)",
            }
            for attr, w in widgets.items():
                if isinstance(w, LabeledSlider):
                    w.valueChanged.connect(lambda v, wh=which, a=attr: self._set_sub(wh, a, v))
                    form.addRow(labels[attr], w)
                else:
                    w.toggled.connect(lambda v, wh=which, a=attr: self._set_sub(wh, a, v))
                    form.addRow(w)
            self._stick_widgets[which] = widgets
            grid.addWidget(box, 0, col)

        trig = QGroupBox("Triggers")
        tform = QFormLayout(trig)
        self.lt_deadzone = LabeledSlider(0.0, 0.5, 0.01, "{:.0%}")
        self.rt_deadzone = LabeledSlider(0.0, 0.5, 0.01, "{:.0%}")
        self.lt_deadzone.valueChanged.connect(lambda v: self._set_sub("left_trigger", "deadzone", v))
        self.rt_deadzone.valueChanged.connect(lambda v: self._set_sub("right_trigger", "deadzone", v))
        tform.addRow("LT deadzone", self.lt_deadzone)
        tform.addRow("RT deadzone", self.rt_deadzone)
        grid.addWidget(trig, 1, 0)

        rumble = QGroupBox("Rumble (force feedback)")
        rform = QFormLayout(rumble)
        self.rumble_enabled = QCheckBox("Forward game rumble to the Wiimote motor")
        self.rumble_enabled.toggled.connect(lambda v: self._set_profile_attr("rumble_enabled", v))
        self.rumble_mode = QComboBox()
        self.rumble_mode.addItem("Variable intensity (pulse-width)", "pwm")
        self.rumble_mode.addItem("On/off above threshold", "threshold")
        self.rumble_mode.currentIndexChanged.connect(
            lambda _i: self._set_profile_attr("rumble_mode", self.rumble_mode.currentData())
        )
        self.rumble_threshold = LabeledSlider(0, 255, 1, "{:.0f}")
        self.rumble_threshold.valueChanged.connect(lambda v: self._set_profile_attr("rumble_threshold", int(v)))
        rform.addRow(self.rumble_enabled)
        rform.addRow("Mode", self.rumble_mode)
        rform.addRow("Threshold", self.rumble_threshold)
        grid.addWidget(rumble, 1, 1)

        app = QGroupBox("Application")
        aform = QFormLayout(app)
        self.app_checks: dict[str, QCheckBox] = {}
        for attr, text in (
            ("minimize_to_tray", "Minimize to system tray"),
            ("close_to_tray", "Closing the window keeps running in the tray"),
            ("start_minimized", "Start minimized to tray"),
            ("notifications", "Show connect / disconnect / low battery notifications"),
            ("auto_start", "Start with Windows"),
        ):
            cb = QCheckBox(text)
            cb.toggled.connect(lambda v, a=attr: self._set_setting(a, v))
            self.app_checks[attr] = cb
            aform.addRow(cb)
        self.grace_spin = QDoubleSpinBox()
        self.grace_spin.setRange(0, 300)
        self.grace_spin.setSuffix(" s")
        self.grace_spin.setToolTip("How long the virtual controller stays plugged in while a dropped Wiimote reconnects")
        self.grace_spin.valueChanged.connect(lambda v: self._set_setting("reconnect_grace_s", float(v)))
        aform.addRow("Reconnect grace period", self.grace_spin)
        self.write_method = QComboBox()
        self.write_method.addItem("Automatic (recommended)", "auto")
        self.write_method.addItem("WriteFile (Microsoft stack)", "hidapi")
        self.write_method.addItem("HidD_SetOutputReport (other stacks / clones)", "set_output_report")
        self.write_method.currentIndexChanged.connect(
            lambda _i: self._set_setting("write_method", self.write_method.currentData())
        )
        aform.addRow("HID write method (advanced)", self.write_method)
        mouse = QGroupBox("Mouse pointer")
        mform = QFormLayout(mouse)
        self.mouse_widgets: dict[str, QWidget] = {
            "speed": LabeledSlider(100, 5000, 50, "{:.0f} px/s"),
            "deadzone": LabeledSlider(0.0, 0.5, 0.01, "{:.0%}"),
            "curve": LabeledSlider(0.5, 3.0, 0.05, "{:.2f}"),
            "smoothing": LabeledSlider(0.0, 0.95, 0.05, "{:.0%}"),
            "absolute_range": LabeledSlider(0.2, 1.0, 0.05, "{:.0%}"),
            "invert_x": QCheckBox("Invert X axis"),
            "invert_y": QCheckBox("Invert Y axis"),
        }
        mlabels = {
            "speed": "Speed (relative mode)",
            "deadzone": "Deadzone (relative mode)",
            "curve": "Acceleration curve (relative)",
            "smoothing": "Smoothing (absolute mode)",
            "absolute_range": "Pointer range (absolute mode)",
        }
        for attr, w in self.mouse_widgets.items():
            if isinstance(w, LabeledSlider):
                w.valueChanged.connect(lambda v, a=attr: self._set_sub("mouse", a, v))
                mform.addRow(mlabels[attr], w)
            else:
                w.toggled.connect(lambda v, a=attr: self._set_sub("mouse", a, v))
                mform.addRow(w)
        self.key_threshold = LabeledSlider(0.1, 0.95, 0.05, "{:.0%}")
        self.key_threshold.valueChanged.connect(lambda v: self._set_sub("key_stick", "threshold", v))
        mform.addRow("Movement key threshold", self.key_threshold)
        grid.addWidget(mouse, 2, 0, 1, 2)

        grid.addWidget(app, 3, 0, 1, 2)
        grid.setRowStretch(4, 1)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        return scroll

    def _build_log_tab(self) -> QWidget:
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(2000)
        self.log_view.setStyleSheet("font-family: Consolas, monospace")
        return self.log_view

    def _build_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(make_app_icon(MUTED), self)
        menu = QMenu()
        self.tray_status = menu.addAction("No Wiimote connected")
        self.tray_status.setEnabled(False)
        menu.addSeparator()
        menu.addAction("Show / hide window", self.toggle_visible)
        menu.addAction("Pair Wiimote…", self._open_pair_dialog)
        menu.addAction("Rescan for Wiimotes", self.manager.rescan)
        self.tray_profiles = menu.addMenu("Profile")
        menu.addSeparator()
        menu.addAction("Quit", self.quit_app)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.setToolTip("Wiimote → XInput")
        self.tray.show()
        self._tray_menu = menu  # keep a reference
        self._refresh_tray_profiles()

    # ------------------------------------------------------------ profiles

    def _refresh_profile_combo(self) -> None:
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(list(self.store.profiles))
        self.profile_combo.setCurrentText(self.store.settings.active_profile)
        self.profile_combo.blockSignals(False)
        if getattr(self, "tray", None):
            self._refresh_tray_profiles()

    def _refresh_tray_profiles(self) -> None:
        self.tray_profiles.clear()
        group = QActionGroup(self.tray_profiles)
        for name in self.store.profiles:
            act = QAction(name, self.tray_profiles, checkable=True)
            act.setChecked(name == self.store.settings.active_profile)
            act.triggered.connect(lambda _c=False, n=name: self._on_profile_selected(n))
            group.addAction(act)
            self.tray_profiles.addAction(act)

    def _on_profile_selected(self, name: str) -> None:
        if not name or name not in self.store.profiles:
            return
        self._flush_save()
        self.store.set_active(name)
        self._profile = self.store.active_profile.copy()
        self.manager.apply_profile(self._profile.copy())
        self._load_profile_into_widgets()
        self._refresh_profile_combo()
        log.info("Active profile: %s", name)

    def _profile_changed(self) -> None:
        """Called after any edit of the working copy."""
        snapshot = self._profile.copy()
        self.store.put_profile(snapshot, save=False)
        self.manager.apply_profile(snapshot)
        self._save_timer.start()

    def _flush_save(self) -> None:
        if self._save_timer.isActive():
            self._save_timer.stop()
            self.store.save()

    def _set_profile_attr(self, attr: str, value) -> None:
        setattr(self._profile, attr, value)
        self._profile_changed()

    def _set_sub(self, which: str, attr: str, value) -> None:
        setattr(getattr(self._profile, which), attr, value)
        self._profile_changed()

    def _set_button(self, source: str, target: str) -> None:
        if target == "NONE":
            self._profile.buttons.pop(source, None)
        else:
            self._profile.buttons[source] = target
        self._profile_changed()

    def _set_key(self, source: str, target: str) -> None:
        if target == "NONE":
            self._profile.keys.pop(source, None)
        else:
            self._profile.keys[source] = target
        self._profile_changed()

    def _ask_name(self, title: str, default: str) -> str | None:
        name, ok = QInputDialog.getText(self, title, "Profile name:", text=default)
        name = name.strip()
        if not ok or not name:
            return None
        if name in self.store.profiles and name != self._profile.name:
            QMessageBox.warning(self, title, f"A profile named “{name}” already exists.")
            return None
        return name

    def _profile_new(self) -> None:
        name = self._ask_name("New profile", self.store.unique_name("My profile"))
        if name:
            self.store.put_profile(Profile(name=name))
            self._on_profile_selected(name)

    def _profile_duplicate(self) -> None:
        name = self._ask_name("Duplicate profile", self.store.unique_name(f"{self._profile.name} copy"))
        if name:
            self.store.put_profile(self._profile.copy(name))
            self._on_profile_selected(name)

    def _profile_rename(self) -> None:
        old = self._profile.name
        name = self._ask_name("Rename profile", old)
        if name and name != old:
            self._flush_save()
            self._profile.name = name
            self.store.put_profile(self._profile.copy(), old_name=old)
            self._refresh_profile_combo()

    def _profile_delete(self) -> None:
        if len(self.store.profiles) <= 1:
            QMessageBox.information(self, "Delete profile", "You cannot delete the last profile.")
            return
        if QMessageBox.question(self, "Delete profile", f"Delete profile “{self._profile.name}”?") != QMessageBox.Yes:
            return
        self._flush_save()
        self.store.delete_profile(self._profile.name)
        self._on_profile_selected(self.store.settings.active_profile)

    def _profile_reset(self) -> None:
        if QMessageBox.question(self, "Reset profile", "Reset all mappings and tuning of this profile?") != QMessageBox.Yes:
            return
        self._profile = Profile(name=self._profile.name)
        self._profile_changed()
        self._load_profile_into_widgets()

    def _profile_import(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Import profile", "", "Profile (*.json)")
        if not path:
            return
        try:
            profile = self.store.import_profile(Path(path))
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Import failed", str(exc))
            return
        self._on_profile_selected(profile.name)

    def _profile_export(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Export profile", f"{self._profile.name}.json", "Profile (*.json)")
        if path:
            try:
                ConfigStore.export_profile(self._profile, Path(path))
            except OSError as exc:
                QMessageBox.critical(self, "Export failed", str(exc))

    # ------------------------------------------------------------ widget sync

    @staticmethod
    def _select_data(combo: QComboBox, data) -> None:
        combo.blockSignals(True)
        idx = combo.findData(data)
        combo.setCurrentIndex(max(0, idx))
        combo.blockSignals(False)

    @staticmethod
    def _set_checked(cb: QCheckBox, value: bool) -> None:
        cb.blockSignals(True)
        cb.setChecked(value)
        cb.blockSignals(False)

    def _load_profile_into_widgets(self) -> None:
        p = self._profile
        self._select_data(self.orientation_combo, p.orientation)
        for source, combo in self._button_combos.items():
            self._select_data(combo, p.buttons.get(source, "NONE"))
        for source, combo in self._key_combos.items():
            self._select_data(combo, p.keys.get(source, "NONE"))
        self._set_checked(self.xinput_enabled, p.xinput_enabled)
        self._select_data(self.mouse_source, p.mouse.source)
        self._select_data(self.mouse_mode, p.mouse.mode)
        self._select_data(self.key_stick_source, p.key_stick.source)
        self._select_data(self.key_stick_layout, p.key_stick.layout)
        for attr, w in self.mouse_widgets.items():
            if isinstance(w, LabeledSlider):
                w.setValue(getattr(p.mouse, attr))
            else:
                self._set_checked(w, getattr(p.mouse, attr))
        self.key_threshold.setValue(p.key_stick.threshold)
        for which, combo in self.stick_combos.items():
            self._select_data(combo, getattr(p, which).source)
        for which, combo in self.trigger_combos.items():
            self._select_data(combo, getattr(p, which).source)
        self._set_checked(self.classic_fallback, p.classic_left_stick_fallback)
        for which, widgets in self._stick_widgets.items():
            cfg = getattr(p, which)
            for attr, w in widgets.items():
                if isinstance(w, LabeledSlider):
                    w.setValue(getattr(cfg, attr))
                else:
                    self._set_checked(w, getattr(cfg, attr))
        self.lt_deadzone.setValue(p.left_trigger.deadzone)
        self.rt_deadzone.setValue(p.right_trigger.deadzone)
        self._set_checked(self.rumble_enabled, p.rumble_enabled)
        self._select_data(self.rumble_mode, p.rumble_mode)
        self.rumble_threshold.setValue(p.rumble_threshold)

    def _load_settings_into_widgets(self) -> None:
        s = self.store.settings
        for attr, cb in self.app_checks.items():
            self._set_checked(cb, getattr(s, attr))
        self.grace_spin.blockSignals(True)
        self.grace_spin.setValue(s.reconnect_grace_s)
        self.grace_spin.blockSignals(False)
        self._select_data(self.write_method, s.write_method)

    def _set_setting(self, attr: str, value) -> None:
        setattr(self.store.settings, attr, value)
        if attr == "auto_start" and not platform_utils.set_autostart(bool(value)) and value:
            log.warning("Auto-start is only supported on Windows")
        self.store.save()
        self.manager.apply_settings(self.store.settings)

    # ------------------------------------------------------------ driver

    def _check_driver(self) -> None:
        if self._dry_run:
            self.driver_banner.set_message(
                "Dry-run mode: Wiimote input is decoded and shown, but no virtual Xbox controller is created.", WARN
            )
            self.install_btn.hide()
            self.recheck_btn.hide()
            self._start_manager()
            return
        status = self._driver_check()
        if status.ok:
            self.driver_banner.set_message("✔ ViGEmBus driver detected — virtual Xbox 360 controllers are available.", OK)
            self.install_btn.hide()
            self.recheck_btn.hide()
            self._start_manager()
        else:
            self.driver_banner.set_message(
                f"<b>Virtual controller driver missing.</b> {status.message}<br>"
                "Install the ViGEmBus driver, then click Re-check (a reboot may be required).",
                BAD,
            )
            self.install_btn.show()
            self.recheck_btn.show()

    def _start_manager(self) -> None:
        if not self._manager_started:
            self.manager.start()
            self._manager_started = True

    def _install_driver(self) -> None:
        installer = find_bundled_installer()
        if installer is None:
            platform_utils.open_url(VIGEMBUS_DOWNLOAD_URL)
            return
        try:
            launch_installer(installer)
        except Exception as exc:  # noqa: BLE001
            QMessageBox.warning(self, "ViGEmBus", f"Could not start the installer ({exc}). Opening the download page.")
            platform_utils.open_url(VIGEMBUS_DOWNLOAD_URL)

    # ------------------------------------------------------------ live view

    def _tick(self) -> None:
        sessions = self.manager.sessions() if self._manager_started else []
        live_keys = {s.hid_info.key for s in sessions}
        for key in [k for k in self._cards if k not in live_keys]:
            card = self._cards.pop(key)
            self.cards_layout.removeWidget(card)
            card.deleteLater()
        for s in sessions:
            key = s.hid_info.key
            card = self._cards.get(key)
            if card is None:
                card = ControllerCard()
                self._cards[key] = card
                self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
            info = s.info()
            card.update_info(info)
            if self.isVisible():
                card.update_live(s.output, s.state, s.kbm)
            self._check_battery(info)
        self.empty_widget.setVisible(not self._cards)
        self._update_tray(sessions)

    def _check_battery(self, info: SessionInfo) -> None:
        if info.battery_low and info.key not in self._low_battery_warned:
            self._low_battery_warned.add(info.key)
            self._notify("Wiimote battery low", f"Player {info.slot}: {info.battery_percent}% remaining.")
        elif not info.battery_low:
            self._low_battery_warned.discard(info.key)

    def _update_tray(self, sessions) -> None:
        if self.tray is None:
            return
        infos = [s.info() for s in sessions]
        summary = tuple((i.slot, i.status, i.battery_percent) for i in infos)
        if summary == self._tray_state:
            return
        self._tray_state = summary
        connected = [x for x in summary if x[1] == ConnectionStatus.CONNECTED]
        if not summary:
            text, color = "No Wiimote connected", MUTED
        else:
            text = ", ".join(
                f"P{slot}: {status.value}" + (f" {bat}%" if bat is not None else "") for slot, status, bat in summary
            )
            color = OK if connected else WARN
        self.tray_status.setText(text)
        self.tray.setToolTip(f"Wiimote → XInput\n{text}")
        self.tray.setIcon(make_app_icon(color))

    def _on_session_event(self, event: str, info: SessionInfo) -> None:
        if event == "added":
            self._notify("Wiimote connected", f"Player {info.slot} ({info.model}) is ready.")
        elif event == "removed":
            self._notify("Wiimote disconnected", f"Player {info.slot} was disconnected.")

    def _notify(self, title: str, text: str) -> None:
        if self.tray is not None and self.store.settings.notifications:
            self.tray.showMessage(title, text, QSystemTrayIcon.Information, 3000)

    # ------------------------------------------------------------ window / tray

    def _open_pair_dialog(self) -> None:
        self.show_window()
        PairDialog(on_paired=self.manager.rescan, parent=self).exec()

    def _about(self) -> None:
        QMessageBox.about(
            self,
            "About Wiimote → XInput",
            f"<b>Wiimote → XInput</b> {__version__}<br>"
            "Emulates an Xbox 360 controller from a Nintendo Wii Remote using the ViGEmBus driver.<br><br>"
            f"Configuration: {self.store.path}",
        )

    def _on_tray_activated(self, reason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self.toggle_visible()

    def toggle_visible(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide()
        else:
            self.show_window()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def changeEvent(self, event) -> None:  # noqa: N802
        if (
            event.type() == event.Type.WindowStateChange
            and self.isMinimized()
            and self.tray is not None
            and self.store.settings.minimize_to_tray
        ):
            QTimer.singleShot(0, self.hide)
        super().changeEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if not self._quitting and self.tray is not None and self.store.settings.close_to_tray:
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self._notify("Still running", "Wiimote → XInput keeps running in the system tray.")
            return
        self.quit_app()
        event.accept()

    def quit_app(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._timer.stop()
        self._flush_save()
        logging.getLogger().removeHandler(self._log_handler)
        self.manager.stop()
        if self.tray is not None:
            self.tray.hide()
        QApplication.quit()


def run_gui(
    store: ConfigStore,
    manager: DeviceManager,
    driver_check: Callable[[], DriverStatus],
    dry_run: bool,
    start_minimized: bool,
) -> int:
    if sys.platform == "win32":
        # Own taskbar identity so Windows uses our icon rather than python.exe's.
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WiimoteXInput.App")
        except (AttributeError, OSError):
            pass
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName("Wiimote XInput")
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow(store, manager, driver_check, dry_run)
    if start_minimized and window.tray is not None:
        log.info("Started minimized to the system tray")
    else:
        window.show()
    code = app.exec()
    window.quit_app()
    return code
