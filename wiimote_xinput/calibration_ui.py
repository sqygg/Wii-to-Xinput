"""Calibration tab: guided calibration procedures and a live sensor monitor."""

from __future__ import annotations

import logging
import math
import time
from typing import Callable

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .bluetooth_core import DeviceManager, WiimoteSession
from .calibration import (
    EMPTY_CALIBRATION,
    AccelCalibrator,
    CalibrationError,
    Calibrator,
    ClassicCalibrator,
    DeadzoneCalibrator,
    DeviceCalibration,
    IrCalibrator,
    NunchukStickCalibrator,
    RawSnapshot,
)
from .config import ConfigStore, Profile
from .constants import BUTTON_SOURCES, IR_MAX_X, IR_MAX_Y, SOURCE_LABELS
from .motion import tilt_angles
from .widgets import ACCENT, BAD, MUTED, OK, WARN

log = logging.getLogger(__name__)

TASKS: list[tuple[str, str, Callable[[WiimoteSession], Calibrator]]] = [
    ("wiimote_accel", "Wiimote accelerometer", lambda s: AccelCalibrator("wiimote", s.factory_calibration[0])),
    ("nunchuk_accel", "Nunchuk accelerometer", lambda s: AccelCalibrator("nunchuk", s.factory_calibration[1].accel)),
    ("nunchuk_stick", "Nunchuk stick", lambda s: NunchukStickCalibrator()),
    ("classic", "Classic Controller", lambda s: ClassicCalibrator()),
    ("ir", "IR pointer", lambda s: IrCalibrator()),
    ("deadzone", "Deadzone helper", lambda s: DeadzoneCalibrator()),
]

# Which DeviceCalibration fields each task owns (to show "custom" vs "factory")
TASK_FIELDS = {
    "wiimote_accel": ("accel",),
    "nunchuk_accel": ("nunchuk_accel",),
    "nunchuk_stick": ("nunchuk_stick",),
    "classic": ("classic_left", "classic_right", "classic_triggers"),
    "ir": ("ir",),
    "deadzone": (),
}


# --------------------------------------------------------------------------
# Live visuals
# --------------------------------------------------------------------------


def _dim(widget: QWidget, alpha: int = 70) -> QColor:
    c = QColor(widget.palette().windowText().color())
    c.setAlpha(alpha)
    return c


class StickPlot(QWidget):
    """Raw stick space (0..255 on both axes): live position, calibrated
    range (dashed) and the range measured so far (filled)."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title = title
        self.raw: tuple[int, int] | None = None
        self.cal: tuple[tuple[int, int, int], tuple[int, int, int]] | None = None
        self.measured: tuple[list[int], list[int]] | None = None
        self.setMinimumSize(QSize(170, 190))

    def set_data(self, raw, cal, measured) -> None:
        self.raw, self.cal, self.measured = raw, cal, measured
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        fg = self.palette().windowText().color()
        size = min(self.width(), self.height() - 22) - 4
        ox, oy = (self.width() - size) / 2, 2

        def pt(x: float, y: float) -> QPointF:  # raw (0..255, +Y up) -> widget
            return QPointF(ox + x / 255 * size, oy + (1 - y / 255) * size)

        p.setPen(QPen(_dim(self), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRect(QRectF(ox, oy, size, size))
        if self.measured is not None:
            lo, hi = self.measured
            if hi[0] > lo[0] and hi[1] > lo[1]:
                fill = QColor(ACCENT)
                fill.setAlpha(45)
                p.setBrush(fill)
                p.setPen(Qt.NoPen)
                p.drawRect(QRectF(pt(lo[0], hi[1]), pt(hi[0], lo[1])))
        if self.cal is not None:
            (xmin, xc, xmax), (ymin, yc, ymax) = self.cal
            pen = QPen(OK, 1.5, Qt.DashLine)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(pt(xmin, ymax), pt(xmax, ymin)))
            p.drawLine(pt(xc - 8, yc), pt(xc + 8, yc))
            p.drawLine(pt(xc, yc - 8), pt(xc, yc + 8))
        if self.raw is not None:
            p.setPen(Qt.NoPen)
            p.setBrush(ACCENT)
            p.drawEllipse(pt(*self.raw), 5, 5)
        p.setPen(fg)
        label = self.title + (f"  raw {self.raw[0]}, {self.raw[1]}" if self.raw else "  (no data)")
        p.drawText(QRectF(0, self.height() - 20, self.width(), 18), Qt.AlignCenter, label)
        p.end()


class TriggerBars(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.values = (0, 0)
        self.ranges = ((0, 31), (0, 31))
        self.setMinimumSize(QSize(70, 190))

    def set_data(self, values, ranges) -> None:
        self.values, self.ranges = values, ranges
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        h = self.height() - 24
        fg = self.palette().windowText().color()
        for i, (label, v, (lo, hi)) in enumerate(zip(("LT", "RT"), self.values, self.ranges)):
            x = 8 + i * 30
            p.setPen(QPen(_dim(self), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(x, 2, 20, h))
            fill_h = h * v / 31
            p.fillRect(QRectF(x + 1, 2 + h - fill_h, 18, fill_h), ACCENT)
            p.setPen(QPen(OK, 1.5, Qt.DashLine))
            for edge in (lo, hi):
                y = 2 + h - h * edge / 31
                p.drawLine(QPointF(x - 3, y), QPointF(x + 23, y))
            p.setPen(fg)
            p.drawText(QRectF(x - 6, h + 4, 32, 18), Qt.AlignCenter, f"{label} {v}")
        p.end()


class IrView(QWidget):
    """What the IR camera sees (1024×768), the pointer, and the calibrated screen area."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.dots = ()
        self.pointer_raw = None
        self.screen = None  # (center, half_range) in raw pointer units
        self.setMinimumSize(QSize(280, 230))

    def set_data(self, dots, pointer_raw, screen) -> None:
        self.dots, self.pointer_raw, self.screen = dots, pointer_raw, screen
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = min(self.width() - 4, (self.height() - 24) * 4 / 3)
        h = w * 3 / 4
        ox, oy = (self.width() - w) / 2, 2
        p.fillRect(QRectF(ox, oy, w, h), QColor("#111827"))

        def from_pointer(px: float, py: float) -> QPointF:
            # Pointer space (-1..1, +Y up, mirrored X) -> camera-image position
            return QPointF(ox + (1 + px) / 2 * w, oy + (1 - py) / 2 * h)

        if self.screen is not None:
            (cx, cy), (hx, hy) = self.screen
            p.setPen(QPen(OK, 1.5, Qt.DashLine))
            p.setBrush(Qt.NoBrush)
            p.drawRect(QRectF(from_pointer(cx - hx, cy + hy), from_pointer(cx + hx, cy - hy)).normalized())
        for d in self.dots:
            # Drawn in pointer space, so the crosshair sits between the two dots
            px, py = 1.0 - 2.0 * d.x / IR_MAX_X, 2.0 * d.y / IR_MAX_Y - 1.0
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#f9fafb"))
            p.drawEllipse(from_pointer(px, py), 6, 6)
        if self.pointer_raw is not None:
            c = from_pointer(*self.pointer_raw)
            p.setPen(QPen(ACCENT, 2))
            p.drawLine(c + QPointF(-10, 0), c + QPointF(10, 0))
            p.drawLine(c + QPointF(0, -10), c + QPointF(0, 10))
        p.setPen(self.palette().windowText().color())
        text = f"{len(self.dots)} IR dot(s) visible" if self.dots else "Sensor bar not visible"
        p.drawText(QRectF(0, self.height() - 20, self.width(), 18), Qt.AlignCenter,
                   text + "   ·   dashed = calibrated screen area")
        p.end()


class AccelBars(QWidget):
    """Calibrated acceleration per axis (-2..+2 g) and total magnitude."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.g = None
        self.setMinimumSize(QSize(260, 120))

    def set_data(self, g) -> None:
        self.g = g
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        fg = self.palette().windowText().color()
        rows = ("X", "Y", "Z", "|a|")
        bar_w = self.width() - 110
        values = list(self.g) + [math.sqrt(sum(v * v for v in self.g))] if self.g else [None] * 4
        for i, (label, v) in enumerate(zip(rows, values)):
            y = 6 + i * 28
            p.setPen(fg)
            p.drawText(QRectF(4, y, 30, 20), Qt.AlignVCenter, label)
            rect = QRectF(36, y + 4, bar_w, 12)
            p.setPen(QPen(_dim(self), 1))
            p.drawRect(rect)
            mid = rect.left() + rect.width() / 2
            p.drawLine(QPointF(mid, rect.top() - 2), QPointF(mid, rect.bottom() + 2))
            if v is None:
                continue
            frac = max(-1.0, min(1.0, v / 2.0))
            fill = QRectF(mid, rect.top() + 1, frac * rect.width() / 2, rect.height() - 2).normalized()
            p.fillRect(fill, ACCENT)
            p.setPen(fg)
            p.drawText(QRectF(rect.right() + 6, y, 70, 20), Qt.AlignVCenter, f"{v:+.2f} g")
        p.end()


# --------------------------------------------------------------------------
# Tab
# --------------------------------------------------------------------------


class CalibrationTab(QWidget):
    def __init__(
        self,
        manager: DeviceManager,
        store: ConfigStore,
        get_profile: Callable[[], Profile],
        profile_edited: Callable[[], None],
        parent=None,
    ):
        super().__init__(parent)
        self.manager = manager
        self.store = store
        self._get_profile = get_profile
        self._profile_edited = profile_edited
        self._session: WiimoteSession | None = None
        self._calibrator: Calibrator | None = None
        self._task_key = TASKS[0][0]
        self._phase = "idle"  # idle | capturing | tracking | done
        self._capture_end = 0.0
        self._device_keys: list[str] = []

        # -- device row
        self.device_combo = QComboBox()
        self.device_combo.setMinimumWidth(300)
        self.device_combo.currentIndexChanged.connect(self._on_device_changed)
        self.reset_all_btn = QPushButton("Reset all to factory")
        self.reset_all_btn.clicked.connect(self._reset_all)
        top = QHBoxLayout()
        top.addWidget(QLabel("Wiimote:"))
        top.addWidget(self.device_combo)
        top.addStretch(1)
        top.addWidget(self.reset_all_btn)

        # -- task list
        self.task_list = QListWidget()
        self.task_list.setMaximumWidth(220)
        for key, label, _factory in TASKS:
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, key)
            self.task_list.addItem(item)
        self.task_list.currentRowChanged.connect(self._on_task_changed)

        # -- wizard panel
        self.title = QLabel()
        tf = self.title.font()
        tf.setPointSizeF(tf.pointSizeF() + 3)
        tf.setBold(True)
        self.title.setFont(tf)
        self.source_label = QLabel()
        self.description = QLabel()
        self.description.setWordWrap(True)
        self.description.setStyleSheet(f"color: {MUTED.name()}")
        self.step_label = QLabel()
        self.instruction = QLabel()
        self.instruction.setWordWrap(True)
        inf = QFont(self.instruction.font())
        inf.setPointSizeF(inf.pointSizeF() + 1.5)
        self.instruction.setFont(inf)
        self.instruction.setMinimumHeight(48)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setMaximumHeight(8)
        self.primary_btn = QPushButton("Capture")
        self.primary_btn.setDefault(True)
        self.primary_btn.clicked.connect(self._on_primary)
        self.restart_btn = QPushButton("Start over")
        self.restart_btn.clicked.connect(self._restart)
        self.save_btn = QPushButton("Save calibration")
        self.save_btn.clicked.connect(self._save)
        self.factory_btn = QPushButton("Reset to factory")
        self.factory_btn.clicked.connect(self._reset_task)
        self.status = QLabel()
        self.status.setWordWrap(True)

        buttons = QHBoxLayout()
        for b in (self.primary_btn, self.restart_btn, self.save_btn, self.factory_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)

        # -- visuals
        self.visuals = QStackedWidget()
        self.visuals.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.accel_view = AccelBars()
        self.stick_view = StickPlot("Nunchuk stick")
        classic_box = QWidget()
        cl = QHBoxLayout(classic_box)
        cl.setContentsMargins(0, 0, 0, 0)
        self.classic_left_view = StickPlot("Left")
        self.classic_right_view = StickPlot("Right")
        self.trigger_view = TriggerBars()
        cl.addWidget(self.classic_left_view)
        cl.addWidget(self.classic_right_view)
        cl.addWidget(self.trigger_view)
        self.ir_view = IrView()
        self._visual_index = {}
        for name, w in (("none", QWidget()), ("accel", self.accel_view), ("stick", self.stick_view),
                        ("classic", classic_box), ("ir", self.ir_view)):
            self._visual_index[name] = self.visuals.addWidget(w)

        wizard = QFrame()
        wizard.setObjectName("card")
        wizard.setFrameShape(QFrame.StyledPanel)
        wl = QVBoxLayout(wizard)
        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.source_label)
        wl.addLayout(head)
        wl.addWidget(self.description)
        wl.addSpacing(6)
        wl.addWidget(self.step_label)
        wl.addWidget(self.instruction)
        wl.addWidget(self.progress)
        wl.addLayout(buttons)
        wl.addWidget(self.status)
        wl.addWidget(self.visuals, 1)

        # -- monitor
        monitor_title = QLabel("<b>Live sensor monitor</b>")
        self.monitor = QLabel("—")
        self.monitor.setTextFormat(Qt.PlainText)
        self.monitor.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.monitor.setStyleSheet("font-family: Consolas, 'DejaVu Sans Mono', monospace")
        self.monitor.setTextInteractionFlags(Qt.TextSelectableByMouse)

        right = QVBoxLayout()
        right.addWidget(wizard, 3)
        right.addWidget(monitor_title)
        right.addWidget(self.monitor, 2)

        body = QHBoxLayout()
        body.addWidget(self.task_list)
        body.addLayout(right, 1)

        root = QVBoxLayout(self)
        root.addLayout(top)
        root.addLayout(body, 1)

        self.task_list.setCurrentRow(0)
        self._show_no_device()

    # ------------------------------------------------------------ devices

    def set_active(self, active: bool) -> None:
        """Raw snapshots are only built while this tab is visible."""
        for s in self.manager.sessions():
            s.raw_enabled = active and s is self._session
        if not active and self._phase in ("capturing", "tracking"):
            self._restart()

    def _refresh_devices(self) -> None:
        sessions = self.manager.sessions()
        keys = [s.hid_info.key for s in sessions]
        if keys == self._device_keys:
            return
        current = self._session.hid_info.key if self._session else None
        self._device_keys = keys
        self.device_combo.blockSignals(True)
        self.device_combo.clear()
        for s in sessions:
            serial = f" ({s.hid_info.serial})" if s.hid_info.serial else ""
            self.device_combo.addItem(f"Player {s.slot} — {s.hid_info.model}{serial}", s.hid_info.key)
        idx = keys.index(current) if current in keys else (0 if keys else -1)
        self.device_combo.setCurrentIndex(idx)
        self.device_combo.blockSignals(False)
        self._on_device_changed(idx)

    def _on_device_changed(self, _idx: int) -> None:
        key = self.device_combo.currentData()
        session = next((s for s in self.manager.sessions() if s.hid_info.key == key), None)
        if self._session is not None and self._session is not session:
            self._session.raw_enabled = False
        self._session = session
        if session is not None:
            session.raw_enabled = self.isVisible()
            self._restart()
        else:
            self._show_no_device()

    def _show_no_device(self) -> None:
        if self._session is None:
            self.status.setText("Connect a Wiimote to calibrate it.")
            self.status.setStyleSheet(f"color: {MUTED.name()}")
            for b in (self.primary_btn, self.save_btn, self.factory_btn, self.restart_btn, self.reset_all_btn):
                b.setEnabled(False)
            self.monitor.setText("No Wiimote connected.")

    def _user_cal(self) -> DeviceCalibration:
        if self._session is None:
            return EMPTY_CALIBRATION
        return self.store.get_calibration(self._session.hid_info.key) or EMPTY_CALIBRATION

    # ------------------------------------------------------------ tasks

    def _on_task_changed(self, row: int) -> None:
        if row < 0:
            return
        self._task_key = TASKS[row][0]
        self._restart()

    def _restart(self) -> None:
        self._phase = "idle"
        self.progress.setValue(0)
        self.status.setText("")
        if self._session is None:
            self._calibrator = None
            self.title.setText(dict((k, label) for k, label, _f in TASKS)[self._task_key])
            self._show_no_device()
            return
        factory = dict((k, f) for k, _l, f in TASKS)[self._task_key]
        self._calibrator = factory(self._session)
        self._calibrator.start_step()
        c = self._calibrator
        self.title.setText(c.title)
        self.description.setText(c.description)
        self.visuals.setCurrentIndex(self._visual_index[c.visual])
        self.reset_all_btn.setEnabled(True)
        self.restart_btn.setEnabled(True)
        self.save_btn.setEnabled(False)
        self.save_btn.setText("Apply to active profile" if c.key == "deadzone" else "Save calibration")
        self.factory_btn.setVisible(c.key != "deadzone")
        self._update_source_label()
        self._show_step()

    def _update_source_label(self) -> None:
        fields = TASK_FIELDS.get(self._task_key, ())
        if not fields:
            self.source_label.setText("")
            return
        custom = any(getattr(self._user_cal(), f) is not None for f in fields)
        color = OK if custom else MUTED
        self.source_label.setText("● Custom calibration in use" if custom else "● Factory calibration in use")
        self.source_label.setStyleSheet(f"color: {color.name()}; font-weight: bold")
        self.factory_btn.setEnabled(custom)

    def _show_step(self) -> None:
        c = self._calibrator
        if c is None:
            return
        if self._phase == "done":
            self.step_label.setText("Complete")
            self.instruction.setText(
                "Calibration measured. Review the result, then click "
                + ("Apply to active profile." if c.key == "deadzone" else "Save calibration.")
            )
            self.primary_btn.setEnabled(False)
            self.primary_btn.setText("Capture")
            return
        step = c.steps[c.step]
        self.step_label.setText(f"Step {c.step + 1} of {len(c.steps)}")
        self.instruction.setText(step.instruction)
        if self._phase == "tracking":
            self.primary_btn.setText("Done")
        else:
            self.primary_btn.setText("Start" if step.mode == "track" else "Capture")

    def _on_primary(self) -> None:
        c = self._calibrator
        if c is None or self._phase == "done":
            return
        step = c.steps[c.step]
        if self._phase == "idle":
            c.start_step()
            self.status.setText("")
            if step.mode == "still":
                self._phase = "capturing"
                self._capture_end = time.monotonic() + step.seconds
                self.primary_btn.setEnabled(False)
                self.status.setText("Measuring… keep still.")
                self.status.setStyleSheet(f"color: {WARN.name()}")
            else:
                self._phase = "tracking"
                self.status.setText("Recording… move as instructed, then click Done.")
                self.status.setStyleSheet(f"color: {WARN.name()}")
            self._show_step()
        elif self._phase == "tracking":
            self._complete_step()

    def _complete_step(self) -> None:
        c = self._calibrator
        assert c is not None
        try:
            finished = c.finish_step()
        except CalibrationError as exc:
            self._phase = "idle"
            self.status.setText(f"✖ {exc}")
            self.status.setStyleSheet(f"color: {BAD.name()}")
            self.primary_btn.setEnabled(True)
            self.progress.setValue(0)
            self._show_step()
            return
        self.progress.setValue(0)
        self.primary_btn.setEnabled(True)
        if finished:
            self._phase = "done"
            self.status.setText(f"✔ {c.summary()}")
            self.status.setStyleSheet(f"color: {OK.name()}")
            self.save_btn.setEnabled(True)
        else:
            self._phase = "idle"
            c.start_step()
            self.status.setText("✔ Step captured.")
            self.status.setStyleSheet(f"color: {OK.name()}")
        self._show_step()

    def _save(self) -> None:
        c, s = self._calibrator, self._session
        if c is None or s is None or self._phase != "done":
            return
        if isinstance(c, DeadzoneCalibrator):
            profile = self._get_profile()
            changes = c.apply_to_profile(profile)
            if changes:
                self._profile_edited()
                self.status.setText(f"✔ Applied to “{profile.name}”: " + ", ".join(changes))
            else:
                self.status.setText(
                    "None of the active profile's sticks or mouse use a measured stick, so nothing was changed. "
                    "Suggested: " + c.summary()
                )
            self.save_btn.setEnabled(False)
            return
        new = c.apply(self._user_cal())
        self.manager.set_calibration(s.hid_info.key, new)
        log.info("Saved %s calibration for %s", c.title, s.hid_info.key)
        hint = ""
        if isinstance(c, IrCalibrator):
            hint = " Tip: with a calibrated pointer, set Settings → Mouse pointer → Pointer range to 100%."
        self.status.setText(f"✔ Saved and applied: {c.summary()}.{hint}")
        self.save_btn.setEnabled(False)
        self._update_source_label()

    def _reset_task(self) -> None:
        c, s = self._calibrator, self._session
        if c is None or s is None or c.key == "deadzone":
            return
        self.manager.set_calibration(s.hid_info.key, c.reset(self._user_cal()))
        self.status.setText("Factory calibration restored.")
        self.status.setStyleSheet(f"color: {MUTED.name()}")
        self._update_source_label()

    def _reset_all(self) -> None:
        s = self._session
        if s is None:
            return
        if QMessageBox.question(self, "Reset calibration",
                                "Remove all custom calibration for this Wiimote?") != QMessageBox.Yes:
            return
        self.manager.set_calibration(s.hid_info.key, None)
        self._restart()
        self.status.setText("All calibration for this Wiimote reset to factory values.")

    # ------------------------------------------------------------ live update

    def tick(self) -> None:
        self._refresh_devices()
        s = self._session
        if s is None:
            return
        s.raw_enabled = True
        raw = s.raw
        c = self._calibrator
        if c is not None:
            reason = c.unavailable_reason(raw)
            if self._phase == "idle":
                self.primary_btn.setEnabled(reason is None)
                if reason and not self.status.text().startswith("✖"):
                    self.status.setText(reason)
                    self.status.setStyleSheet(f"color: {MUTED.name()}")
            if raw is not None and self._phase in ("capturing", "tracking"):
                c.feed(raw)
            if self._phase == "capturing":
                step = c.steps[c.step]
                remaining = self._capture_end - time.monotonic()
                self.progress.setValue(int(100 * (1 - max(0.0, remaining) / step.seconds)))
                if remaining <= 0:
                    self._complete_step()
        self._update_visual(raw)
        self._update_monitor(s, raw)

    def _update_visual(self, raw: RawSnapshot | None) -> None:
        c, s = self._calibrator, self._session
        if c is None or s is None:
            return
        cal = s.calibration
        if c.visual == "accel":
            st = raw.state if raw else None
            g = None
            if st is not None:
                g = st.accel if c.key == "wiimote_accel" else (st.nunchuk.accel if st.nunchuk else None)
            self.accel_view.set_data(g)
        elif c.visual == "stick":
            n = cal.nunchuk
            tracker = getattr(c, "tracker", None)
            measured = (tracker.lo, tracker.hi) if tracker and self._phase in ("tracking", "done") else None
            self.stick_view.set_data(
                raw.nunchuk.stick if raw and raw.nunchuk else None,
                ((n.stick_x.minimum, n.stick_x.center, n.stick_x.maximum),
                 (n.stick_y.minimum, n.stick_y.center, n.stick_y.maximum)),
                measured,
            )
        elif c.visual == "classic":
            k = cal.classic
            cr = raw.classic if raw else None
            show = self._phase in ("tracking", "done")
            self.classic_left_view.set_data(
                cr.left if cr else None,
                ((k.lx.minimum, k.lx.center, k.lx.maximum), (k.ly.minimum, k.ly.center, k.ly.maximum)),
                (c.left.lo, c.left.hi) if show else None,
            )
            self.classic_right_view.set_data(
                cr.right if cr else None,
                ((k.rx.minimum, k.rx.center, k.rx.maximum), (k.ry.minimum, k.ry.center, k.ry.maximum)),
                (c.right.lo, c.right.hi) if show else None,
            )
            self.trigger_view.set_data((cr.lt, cr.rt) if cr else (0, 0), (k.lt_range, k.rt_range))
        elif c.visual == "ir":
            ir = self._user_cal().ir
            self.ir_view.set_data(
                raw.ir_dots if raw else (),
                raw.pointer_raw if raw else None,
                (ir.center, ir.half_range) if ir else None,
            )

    def _update_monitor(self, s: WiimoteSession, raw: RawSnapshot | None) -> None:
        if raw is None:
            self.monitor.setText("Waiting for data…")
            return
        st = raw.state
        profile = self._get_profile()
        sideways = profile.orientation == "sideways"
        lines = []
        if raw.accel and st.accel:
            g = st.accel
            mag = math.sqrt(sum(v * v for v in g))
            lr, ud = (math.degrees(a) for a in tilt_angles(g, sideways))
            lines.append(f"Wiimote accel  raw {raw.accel[0]:4d} {raw.accel[1]:4d} {raw.accel[2]:4d}"
                         f"  →  {g[0]:+.2f} {g[1]:+.2f} {g[2]:+.2f} g   |a| {mag:.2f} g")
            lines.append(f"               tilt  left/right {lr:+5.1f}°  up/down {ud:+5.1f}°"
                         f"   (gesture at ±{profile.motion.tilt_angle_deg:.0f}°)")
            lines.append(f"               shake |a|−1g = {abs(mag - 1):.2f} g"
                         f"   (gesture at ≥ {profile.motion.shake_threshold_g:.2f} g)")
        if raw.nunchuk and st.nunchuk:
            n, ns = raw.nunchuk, st.nunchuk
            lines.append(f"Nunchuk stick  raw {n.stick[0]:4d} {n.stick[1]:4d}"
                         f"  →  {ns.stick[0]:+.2f} {ns.stick[1]:+.2f}")
            if ns.accel:
                a = ns.accel
                lines.append(f"Nunchuk accel  raw {n.accel[0]:4d} {n.accel[1]:4d} {n.accel[2]:4d}"
                             f"  →  {a[0]:+.2f} {a[1]:+.2f} {a[2]:+.2f} g")
        if raw.classic and st.classic:
            c, cs = raw.classic, st.classic
            lines.append(f"Classic L      raw {c.left[0]:4d} {c.left[1]:4d}  →  {cs.left[0]:+.2f} {cs.left[1]:+.2f}")
            lines.append(f"Classic R      raw {c.right[0]:4d} {c.right[1]:4d}  →  {cs.right[0]:+.2f} {cs.right[1]:+.2f}")
            lines.append(f"Triggers       raw L {c.lt:2d} R {c.rt:2d}  →  {cs.lt:.2f} {cs.rt:.2f}")
        if raw.ir_dots or raw.pointer_raw is not None:
            pr, pc = raw.pointer_raw, st.pointer
            lines.append(f"IR             {len(raw.ir_dots)} dot(s)"
                         + (f"  pointer raw {pr[0]:+.2f} {pr[1]:+.2f}" if pr else "")
                         + (f"  →  {pc[0]:+.2f} {pc[1]:+.2f}" if pc else ""))
        elif s.info().ir_enabled:
            lines.append("IR             sensor bar not visible")
        else:
            lines.append("IR             camera off (select a profile that uses the IR pointer)")
        active = [SOURCE_LABELS[name] for name, (group, mask) in BUTTON_SOURCES.items()
                  if group == "motion" and st.motion & mask]
        lines.append("Gestures       " + (", ".join(active) if active else "—"))
        user = self._user_cal()
        custom = [label for key, label, _f in TASKS
                  if TASK_FIELDS[key] and any(getattr(user, f) is not None for f in TASK_FIELDS[key])]
        lines.append("Custom calib.  " + (", ".join(custom) if custom else "none (factory values)"))
        self.monitor.setText("\n".join(lines))
