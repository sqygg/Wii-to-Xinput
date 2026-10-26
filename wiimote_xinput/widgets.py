"""Custom Qt widgets: controller cards, live XInput visualiser, sliders, icon."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QSizePolicy,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from .bluetooth_core import ConnectionStatus, SessionInfo
from .constants import (
    BTN_A,
    BTN_B,
    BTN_DOWN,
    BTN_HOME,
    BTN_LEFT,
    BTN_MINUS,
    BTN_ONE,
    BTN_PLUS,
    BTN_RIGHT,
    BTN_TWO,
    BTN_UP,
    XUSB_A,
    XUSB_B,
    XUSB_BACK,
    XUSB_DPAD_DOWN,
    XUSB_DPAD_LEFT,
    XUSB_DPAD_RIGHT,
    XUSB_DPAD_UP,
    XUSB_GUIDE,
    XUSB_LEFT_SHOULDER,
    XUSB_LEFT_THUMB,
    XUSB_RIGHT_SHOULDER,
    XUSB_RIGHT_THUMB,
    XUSB_START,
    XUSB_X,
    XUSB_Y,
)
from .mapper import NEUTRAL, KbmState, XInputState
from .parser import WiimoteState

ACCENT = QColor("#3b82f6")
OK = QColor("#22c55e")
WARN = QColor("#f59e0b")
BAD = QColor("#ef4444")
MUTED = QColor("#6b7280")

STATUS_COLORS = {
    ConnectionStatus.CONNECTED: OK,
    ConnectionStatus.CONNECTING: WARN,
    ConnectionStatus.RECONNECTING: WARN,
    ConnectionStatus.DISCONNECTED: BAD,
}


def make_app_icon(dot: QColor | None = None, size: int = 64) -> QIcon:
    """Draw the app/tray icon: a Wiimote silhouette with a status dot."""
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 64.0
    body = QRectF(20 * s, 4 * s, 24 * s, 56 * s)
    p.setPen(QPen(QColor("#1f2937"), 2.5 * s))
    p.setBrush(QColor("#f9fafb"))
    p.drawRoundedRect(body, 9 * s, 9 * s)
    # D-pad
    p.setBrush(QColor("#374151"))
    p.setPen(Qt.NoPen)
    p.drawRect(QRectF(29 * s, 11 * s, 6 * s, 16 * s))
    p.drawRect(QRectF(24 * s, 16 * s, 16 * s, 6 * s))
    # A button
    p.setBrush(QColor("#9ca3af"))
    p.drawEllipse(QPointF(32 * s, 35 * s), 4 * s, 4 * s)
    if dot is not None:
        p.setBrush(dot)
        p.setPen(QPen(QColor("white"), 2 * s))
        p.drawEllipse(QPointF(48 * s, 50 * s), 10 * s, 10 * s)
    p.end()
    return QIcon(pm)


class LabeledSlider(QWidget):
    """Horizontal slider for a float value, with a live value label."""

    valueChanged = Signal(float)

    def __init__(self, minimum: float, maximum: float, step: float, fmt: str = "{:.2f}", parent=None):
        super().__init__(parent)
        self._step = step
        self._min = minimum
        self._fmt = fmt
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, round((maximum - minimum) / step))
        self.label = QLabel()
        self.label.setMinimumWidth(48)
        self.label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.label)
        self.slider.valueChanged.connect(self._changed)
        self._changed(self.slider.value())

    def value(self) -> float:
        return self._min + self.slider.value() * self._step

    def setValue(self, v: float) -> None:
        self.slider.blockSignals(True)
        self.slider.setValue(round((v - self._min) / self._step))
        self.slider.blockSignals(False)
        self.label.setText(self._fmt.format(self.value()))

    def _changed(self, _raw: int) -> None:
        v = self.value()
        self.label.setText(self._fmt.format(v))
        self.valueChanged.emit(v)


class XInputView(QWidget):
    """Paints what the game sees: sticks, triggers and buttons."""

    BUTTONS = [
        ("A", XUSB_A, QColor("#22c55e")),
        ("B", XUSB_B, QColor("#ef4444")),
        ("X", XUSB_X, QColor("#3b82f6")),
        ("Y", XUSB_Y, QColor("#eab308")),
        ("LB", XUSB_LEFT_SHOULDER, ACCENT),
        ("RB", XUSB_RIGHT_SHOULDER, ACCENT),
        ("Back", XUSB_BACK, ACCENT),
        ("Start", XUSB_START, ACCENT),
        ("Guide", XUSB_GUIDE, ACCENT),
        ("LS", XUSB_LEFT_THUMB, ACCENT),
        ("RS", XUSB_RIGHT_THUMB, ACCENT),
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = NEUTRAL
        self.setMinimumSize(QSize(380, 130))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_state(self, state: XInputState) -> None:
        if state != self._state:
            self._state = state
            self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt naming
        st = self._state
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        fg = pal.windowText().color()
        dim = QColor(fg)
        dim.setAlpha(60)
        h = self.height()
        r = (h - 30) / 2.0

        def stick(cx: float, x: int, y: int, label: str) -> None:
            c = QPointF(cx, 8 + r)
            p.setPen(QPen(dim, 1.5))
            p.setBrush(Qt.NoBrush)
            p.drawEllipse(c, r, r)
            p.drawLine(QPointF(cx - r, c.y()), QPointF(cx + r, c.y()))
            p.drawLine(QPointF(cx, c.y() - r), QPointF(cx, c.y() + r))
            dot = QPointF(cx + x / 32767.0 * r, c.y() - y / 32767.0 * r)
            p.setPen(Qt.NoPen)
            p.setBrush(ACCENT)
            p.drawEllipse(dot, 6, 6)
            p.setPen(fg)
            p.drawText(QRectF(cx - r, h - 20, 2 * r, 18), Qt.AlignCenter, label)

        def trigger(x: float, v: int, label: str) -> None:
            rect = QRectF(x, 8, 12, 2 * r)
            p.setPen(QPen(dim, 1.2))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(rect, 3, 3)
            fill = rect.adjusted(1, 1 + (2 * r - 2) * (1 - v / 255.0), -1, -1)
            p.setPen(Qt.NoPen)
            p.setBrush(ACCENT)
            p.drawRoundedRect(fill, 2, 2)
            p.setPen(fg)
            p.drawText(QRectF(x - 8, h - 20, 28, 18), Qt.AlignCenter, label)

        trigger(4, st.lt, "LT")
        stick(26 + r, st.lx, st.ly, "L")
        stick(36 + 3 * r, st.rx, st.ry, "R")
        trigger(46 + 4 * r, st.rt, "RT")

        # D-pad
        ox = 72 + 4 * r
        oy = 8 + r
        size = 12
        for mask, dx, dy in (
            (XUSB_DPAD_UP, 0, -1),
            (XUSB_DPAD_DOWN, 0, 1),
            (XUSB_DPAD_LEFT, -1, 0),
            (XUSB_DPAD_RIGHT, 1, 0),
        ):
            rect = QRectF(ox + 14 + dx * size - size / 2, oy + dy * size - size / 2, size, size)
            p.setPen(QPen(dim, 1))
            p.setBrush(ACCENT if st.buttons & mask else Qt.NoBrush)
            p.drawRect(rect)

        # Buttons as pills
        f = QFont(self.font())
        f.setPointSizeF(max(7.0, f.pointSizeF() - 1))
        p.setFont(f)
        bx0 = ox + 40
        avail = max(120.0, self.width() - bx0 - 4)
        cols = 4 if avail > 200 else 3
        pw = avail / cols - 4
        for i, (label, mask, color) in enumerate(self.BUTTONS):
            row, col = divmod(i, cols)
            rect = QRectF(bx0 + col * (pw + 4), 6 + row * 26, pw, 22)
            pressed = bool(st.buttons & mask)
            p.setPen(QPen(color if pressed else dim, 1.2))
            p.setBrush(color if pressed else Qt.NoBrush)
            p.drawRoundedRect(rect, 11, 11)
            p.setPen(QColor("white") if pressed else fg)
            p.drawText(rect, Qt.AlignCenter, label)
        p.end()


WIIMOTE_BUTTON_NAMES = [
    (BTN_UP, "↑"), (BTN_DOWN, "↓"), (BTN_LEFT, "←"), (BTN_RIGHT, "→"),
    (BTN_A, "A"), (BTN_B, "B"), (BTN_ONE, "1"), (BTN_TWO, "2"),
    (BTN_MINUS, "−"), (BTN_HOME, "⌂"), (BTN_PLUS, "+"),
]


def describe_input(state: WiimoteState) -> str:
    parts = [name for mask, name in WIIMOTE_BUTTON_NAMES if state.buttons & mask]
    if state.nunchuk:
        if state.nunchuk.c:
            parts.append("C")
        if state.nunchuk.z:
            parts.append("Z")
    if state.classic and state.classic.buttons:
        parts.append(f"CC:{state.classic.buttons:04X}")
    text = " ".join(parts) or "—"
    if state.accel:
        gx, gy, gz = state.accel
        text += f"    accel ({gx:+.2f}, {gy:+.2f}, {gz:+.2f}) g"
    if state.pointer:
        text += f"    IR ({state.pointer[0]:+.2f}, {state.pointer[1]:+.2f})"
    return text


def describe_kbm(kbm: KbmState) -> str:
    parts = sorted(kbm.keys)
    vx, vy = kbm.mouse_velocity
    if vx or vy:
        parts.append(f"move ({vx:+.2f}, {vy:+.2f})")
    if kbm.mouse_absolute is not None:
        parts.append(f"cursor ({kbm.mouse_absolute[0]:+.2f}, {kbm.mouse_absolute[1]:+.2f})")
    return " ".join(parts) or "—"


class ControllerCard(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("card")
        self.setFrameShape(QFrame.StyledPanel)
        self.title = QLabel()
        tf = self.title.font()
        tf.setPointSizeF(tf.pointSizeF() + 2)
        tf.setBold(True)
        self.title.setFont(tf)
        self.status = QLabel()
        self.battery = QProgressBar()
        self.battery.setRange(0, 100)
        self.battery.setFormat("Battery %p%")
        self.battery.setMaximumWidth(180)
        self.extension = QLabel()
        self.rate = QLabel()
        self.rate.setStyleSheet(f"color: {MUTED.name()}")
        self.raw = QLabel("—")
        self.raw.setStyleSheet(f"color: {MUTED.name()}; font-family: Consolas, monospace")
        self.view = XInputView()
        self.kbm = QLabel("—")
        self.kbm.setStyleSheet(f"color: {MUTED.name()}; font-family: Consolas, monospace")

        head = QHBoxLayout()
        head.addWidget(self.title)
        head.addStretch(1)
        head.addWidget(self.status)
        grid = QGridLayout()
        grid.addWidget(QLabel("Extension:"), 0, 0)
        grid.addWidget(self.extension, 0, 1)
        grid.addWidget(self.battery, 0, 2, Qt.AlignRight)
        grid.addWidget(QLabel("Wiimote input:"), 1, 0)
        grid.addWidget(self.raw, 1, 1, 1, 2)
        grid.setColumnStretch(1, 1)

        lay = QVBoxLayout(self)
        lay.addLayout(head)
        lay.addLayout(grid)
        lay.addWidget(QLabel("Xbox controller output (what games see):"))
        lay.addWidget(self.view)
        kbm_row = QHBoxLayout()
        kbm_row.addWidget(QLabel("Keyboard / mouse output:"))
        kbm_row.addWidget(self.kbm, 1)
        lay.addLayout(kbm_row)
        lay.addWidget(self.rate, 0, Qt.AlignRight)

    def update_info(self, info: SessionInfo) -> None:
        self.title.setText(f"Player {info.slot} — {info.model}")
        color = STATUS_COLORS.get(info.status, MUTED)
        self.status.setText(f"● {info.status.value}")
        self.status.setStyleSheet(f"color: {color.name()}; font-weight: bold")
        if info.battery_percent is None:
            self.battery.setValue(0)
            self.battery.setFormat("Battery ?")
        else:
            self.battery.setValue(info.battery_percent)
            self.battery.setFormat("Battery %p%" + (" (LOW)" if info.battery_low else ""))
        chunk = BAD if info.battery_low or (info.battery_percent or 0) < 20 else OK
        self.battery.setStyleSheet(f"QProgressBar::chunk {{ background: {chunk.name()}; }}")
        ext = info.extension.label + ("  ·  IR on" if info.ir_enabled else "")
        self.extension.setText(ext)
        serial = f"  ·  {info.serial}" if info.serial else ""
        self.rate.setText(f"{info.report_rate_hz:.0f} reports/s{serial}")

    def update_live(self, output: XInputState, state: WiimoteState, kbm: KbmState | None = None) -> None:
        self.view.set_state(output)
        self.raw.setText(describe_input(state))
        self.kbm.setText(describe_kbm(kbm) if kbm is not None else "—")


class Banner(QFrame):
    """Coloured message strip with optional action widgets."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self._lay = QHBoxLayout(self)
        self._lay.addWidget(self.label, 1)

    def add_widget(self, w: QWidget) -> None:
        self._lay.addWidget(w)

    def set_message(self, text: str, color: QColor) -> None:
        self.label.setText(text)
        bg = QColor(color)
        bg.setAlpha(40)
        self.setStyleSheet(
            f"Banner {{ background: rgba({bg.red()},{bg.green()},{bg.blue()},{bg.alpha()});"
            f" border: 1px solid {color.name()}; border-radius: 6px; }}"
        )


def empty_state_widget() -> QWidget:
    w = QLabel(
        "<h3>No Wiimote connected</h3>"
        "<p>Pair your Wiimote (button <b>Pair Wiimote…</b> above, or Windows Bluetooth settings),"
        " then press any button on it. It will show up here automatically.</p>"
    )
    w.setAlignment(Qt.AlignCenter)
    w.setWordWrap(True)
    return w
