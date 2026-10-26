"""Wiimote protocol constants and XInput (XUSB) definitions.

Protocol references: https://wiibrew.org/wiki/Wiimote and
https://wiibrew.org/wiki/Wiimote/Extension_Controllers
"""

from __future__ import annotations

from enum import Enum, IntEnum
from typing import NamedTuple

# --------------------------------------------------------------------------
# Device identification
# --------------------------------------------------------------------------

NINTENDO_VID = 0x057E
WIIMOTE_PID = 0x0306  # Nintendo RVL-CNT-01 (original)
WIIMOTE_TR_PID = 0x0330  # Nintendo RVL-CNT-01-TR (Wii Remote Plus)
WIIMOTE_PIDS = (WIIMOTE_PID, WIIMOTE_TR_PID)
WIIMOTE_BT_NAMES = ("Nintendo RVL-CNT-01", "Nintendo RVL-CNT-01-TR")
BALANCE_BOARD_BT_NAME = "Nintendo RVL-WBC-01"

# Largest Wiimote output report (0x16 write memory) including the report ID.
# Windows requires output reports to be exactly this long.
OUTPUT_REPORT_SIZE = 22
INPUT_REPORT_SIZE = 22


class OutputReport(IntEnum):
    RUMBLE = 0x10
    LEDS = 0x11
    DATA_REPORTING_MODE = 0x12
    IR_CAMERA_ENABLE = 0x13
    SPEAKER_ENABLE = 0x14
    STATUS_REQUEST = 0x15
    WRITE_MEMORY = 0x16
    READ_MEMORY = 0x17
    SPEAKER_DATA = 0x18
    SPEAKER_MUTE = 0x19
    IR_CAMERA_ENABLE_2 = 0x1A


class InputReport(IntEnum):
    STATUS = 0x20
    READ_MEMORY_DATA = 0x21
    ACKNOWLEDGE = 0x22
    CORE = 0x30
    CORE_ACCEL = 0x31
    CORE_EXT8 = 0x32
    CORE_ACCEL_IR12 = 0x33
    CORE_EXT19 = 0x34
    CORE_ACCEL_EXT16 = 0x35
    CORE_IR10_EXT9 = 0x36
    CORE_ACCEL_IR10_EXT6 = 0x37
    EXT21 = 0x3D


class ReportLayout(NamedTuple):
    """Byte offsets inside a data report payload (i.e. after the report ID)."""

    has_buttons: bool
    accel_offset: int | None
    ir_offset: int | None
    ir_length: int
    ext_offset: int | None
    ext_length: int


REPORT_LAYOUTS: dict[int, ReportLayout] = {
    0x30: ReportLayout(True, None, None, 0, None, 0),
    0x31: ReportLayout(True, 2, None, 0, None, 0),
    0x32: ReportLayout(True, None, None, 0, 2, 8),
    0x33: ReportLayout(True, 2, 5, 12, None, 0),
    0x34: ReportLayout(True, None, None, 0, 2, 19),
    0x35: ReportLayout(True, 2, None, 0, 5, 16),
    0x36: ReportLayout(True, None, 2, 10, 12, 9),
    0x37: ReportLayout(True, 2, 5, 10, 15, 6),
    0x3D: ReportLayout(False, None, None, 0, 0, 21),
}

# Reporting modes we request. Bit 2 of the first payload byte = continuous.
MODE_CONTINUOUS_FLAG = 0x04
MODE_DEFAULT = InputReport.CORE_ACCEL_EXT16  # buttons + accel + 16 extension bytes
MODE_WITH_IR = InputReport.CORE_ACCEL_IR10_EXT6  # buttons + accel + basic IR + 6 ext bytes

# --------------------------------------------------------------------------
# Core buttons. Combined mask = (payload[0] << 8) | payload[1]
# --------------------------------------------------------------------------

BTN_LEFT = 0x0100
BTN_RIGHT = 0x0200
BTN_DOWN = 0x0400
BTN_UP = 0x0800
BTN_PLUS = 0x1000
BTN_TWO = 0x0001
BTN_ONE = 0x0002
BTN_B = 0x0004
BTN_A = 0x0008
BTN_MINUS = 0x0010
BTN_HOME = 0x0080
CORE_BUTTON_MASK = 0x1F9F  # the remaining bits carry accelerometer LSBs

# Nunchuk buttons (our own normalised, active-high mask)
NUN_Z = 0x01
NUN_C = 0x02

# Motion gestures (our own mask, produced by motion.MotionDetector)
MOT_WIIMOTE_SHAKE = 0x0001
MOT_WIIMOTE_TILT_LEFT = 0x0002
MOT_WIIMOTE_TILT_RIGHT = 0x0004
MOT_WIIMOTE_TILT_UP = 0x0008
MOT_WIIMOTE_TILT_DOWN = 0x0010
MOT_NUNCHUK_SHAKE = 0x0100
MOT_NUNCHUK_TILT_LEFT = 0x0200
MOT_NUNCHUK_TILT_RIGHT = 0x0400
MOT_NUNCHUK_TILT_UP = 0x0800
MOT_NUNCHUK_TILT_DOWN = 0x1000

# Classic Controller buttons. Combined mask = ~((ext[4] << 8) | ext[5])
CC_RIGHT = 0x8000
CC_DOWN = 0x4000
CC_L = 0x2000
CC_MINUS = 0x1000
CC_HOME = 0x0800
CC_PLUS = 0x0400
CC_R = 0x0200
CC_ZL = 0x0080
CC_B = 0x0040
CC_Y = 0x0020
CC_A = 0x0010
CC_X = 0x0008
CC_ZR = 0x0004
CC_LEFT = 0x0002
CC_UP = 0x0001
CLASSIC_BUTTON_MASK = 0xFEFF

# --------------------------------------------------------------------------
# Memory map
# --------------------------------------------------------------------------

MEM_SPACE_EEPROM = 0x00
MEM_SPACE_REGISTER = 0x04

EEPROM_ACCEL_CALIBRATION = 0x0016  # 10 bytes
REG_EXT_INIT_1 = 0xA400F0  # write 0x55
REG_EXT_INIT_2 = 0xA400FB  # write 0x00 (disables extension encryption)
REG_EXT_ID = 0xA400FA  # 6 bytes
REG_EXT_CALIBRATION = 0xA40020  # 16 bytes

REG_IR_CONTROL = 0xB00030
REG_IR_SENSITIVITY_1 = 0xB00000
REG_IR_SENSITIVITY_2 = 0xB0001A
REG_IR_MODE = 0xB00033
IR_MODE_BASIC = 0x01
# "Wii level 3" sensitivity (marcan), works well with standard sensor bars.
IR_SENSITIVITY_BLOCK_1 = bytes([0x02, 0x00, 0x00, 0x71, 0x01, 0x00, 0xAA, 0x00, 0x64])
IR_SENSITIVITY_BLOCK_2 = bytes([0x63, 0x03])
IR_MAX_X = 1023
IR_MAX_Y = 767

# Memory read error codes (low nibble of the SE byte in report 0x21)
MEM_ERROR_WRITE_ONLY = 0x07
MEM_ERROR_NONEXISTENT = 0x08

# Player LEDs (upper nibble of the LED report payload)
PLAYER_LEDS = {1: 0x10, 2: 0x20, 3: 0x40, 4: 0x80}
MAX_PLAYERS = 4

BATTERY_FULL_RAW = 0xC8


class ExtensionType(str, Enum):
    NONE = "none"
    NUNCHUK = "nunchuk"
    CLASSIC = "classic"
    CLASSIC_PRO = "classic_pro"
    MOTION_PLUS = "motion_plus"
    PARTIAL = "partially_inserted"
    UNKNOWN = "unknown"

    @property
    def label(self) -> str:
        return {
            ExtensionType.NONE: "None",
            ExtensionType.NUNCHUK: "Nunchuk",
            ExtensionType.CLASSIC: "Classic Controller",
            ExtensionType.CLASSIC_PRO: "Classic Controller Pro",
            ExtensionType.MOTION_PLUS: "Wii MotionPlus (unsupported)",
            ExtensionType.PARTIAL: "Partially inserted",
            ExtensionType.UNKNOWN: "Unsupported extension",
        }[self]

    @property
    def is_classic(self) -> bool:
        return self in (ExtensionType.CLASSIC, ExtensionType.CLASSIC_PRO)


def identify_extension(ident: bytes) -> ExtensionType:
    """Identify an extension from the 6 bytes read at 0xA400FA."""
    if len(ident) < 6:
        return ExtensionType.UNKNOWN
    if all(b == 0xFF for b in ident):
        return ExtensionType.PARTIAL
    if all(b == 0x00 for b in ident):
        return ExtensionType.NONE
    tail = bytes(ident[2:6])
    if tail == b"\xa4\x20\x00\x00":
        return ExtensionType.NUNCHUK  # some third-party nunchuks report FF 00 A4 20 00 00
    if tail == b"\xa4\x20\x01\x01":
        return ExtensionType.CLASSIC_PRO if ident[0] == 0x01 else ExtensionType.CLASSIC
    if tail[2:] == b"\x04\x05":
        return ExtensionType.MOTION_PLUS
    return ExtensionType.UNKNOWN


# --------------------------------------------------------------------------
# XInput / XUSB definitions (values match XINPUT_GAMEPAD_* / XUSB_GAMEPAD_*)
# --------------------------------------------------------------------------

XUSB_DPAD_UP = 0x0001
XUSB_DPAD_DOWN = 0x0002
XUSB_DPAD_LEFT = 0x0004
XUSB_DPAD_RIGHT = 0x0008
XUSB_START = 0x0010
XUSB_BACK = 0x0020
XUSB_LEFT_THUMB = 0x0040
XUSB_RIGHT_THUMB = 0x0080
XUSB_LEFT_SHOULDER = 0x0100
XUSB_RIGHT_SHOULDER = 0x0200
XUSB_GUIDE = 0x0400
XUSB_A = 0x1000
XUSB_B = 0x2000
XUSB_X = 0x4000
XUSB_Y = 0x8000

# Mapping targets: name -> (button mask, trigger name or None)
XINPUT_TARGETS: dict[str, tuple[int, str | None]] = {
    "NONE": (0, None),
    "A": (XUSB_A, None),
    "B": (XUSB_B, None),
    "X": (XUSB_X, None),
    "Y": (XUSB_Y, None),
    "LB": (XUSB_LEFT_SHOULDER, None),
    "RB": (XUSB_RIGHT_SHOULDER, None),
    "LT": (0, "lt"),
    "RT": (0, "rt"),
    "BACK": (XUSB_BACK, None),
    "START": (XUSB_START, None),
    "GUIDE": (XUSB_GUIDE, None),
    "LS": (XUSB_LEFT_THUMB, None),
    "RS": (XUSB_RIGHT_THUMB, None),
    "DPAD_UP": (XUSB_DPAD_UP, None),
    "DPAD_DOWN": (XUSB_DPAD_DOWN, None),
    "DPAD_LEFT": (XUSB_DPAD_LEFT, None),
    "DPAD_RIGHT": (XUSB_DPAD_RIGHT, None),
}

XINPUT_TARGET_LABELS = {
    "NONE": "(unbound)",
    "A": "A",
    "B": "B",
    "X": "X",
    "Y": "Y",
    "LB": "Left Bumper (LB)",
    "RB": "Right Bumper (RB)",
    "LT": "Left Trigger (LT)",
    "RT": "Right Trigger (RT)",
    "BACK": "Back / View",
    "START": "Start / Menu",
    "GUIDE": "Guide (Xbox button)",
    "LS": "Left Stick Click (LS)",
    "RS": "Right Stick Click (RS)",
    "DPAD_UP": "D-Pad Up",
    "DPAD_DOWN": "D-Pad Down",
    "DPAD_LEFT": "D-Pad Left",
    "DPAD_RIGHT": "D-Pad Right",
}

# Mapping sources: name -> (state group, bit mask)
BUTTON_SOURCES: dict[str, tuple[str, int]] = {
    "wiimote.UP": ("core", BTN_UP),
    "wiimote.DOWN": ("core", BTN_DOWN),
    "wiimote.LEFT": ("core", BTN_LEFT),
    "wiimote.RIGHT": ("core", BTN_RIGHT),
    "wiimote.A": ("core", BTN_A),
    "wiimote.B": ("core", BTN_B),
    "wiimote.ONE": ("core", BTN_ONE),
    "wiimote.TWO": ("core", BTN_TWO),
    "wiimote.PLUS": ("core", BTN_PLUS),
    "wiimote.MINUS": ("core", BTN_MINUS),
    "wiimote.HOME": ("core", BTN_HOME),
    "nunchuk.C": ("nunchuk", NUN_C),
    "nunchuk.Z": ("nunchuk", NUN_Z),
    "classic.UP": ("classic", CC_UP),
    "classic.DOWN": ("classic", CC_DOWN),
    "classic.LEFT": ("classic", CC_LEFT),
    "classic.RIGHT": ("classic", CC_RIGHT),
    "classic.A": ("classic", CC_A),
    "classic.B": ("classic", CC_B),
    "classic.X": ("classic", CC_X),
    "classic.Y": ("classic", CC_Y),
    "classic.L": ("classic", CC_L),
    "classic.R": ("classic", CC_R),
    "classic.ZL": ("classic", CC_ZL),
    "classic.ZR": ("classic", CC_ZR),
    "classic.PLUS": ("classic", CC_PLUS),
    "classic.MINUS": ("classic", CC_MINUS),
    "classic.HOME": ("classic", CC_HOME),
    "motion.WIIMOTE_SHAKE": ("motion", MOT_WIIMOTE_SHAKE),
    "motion.WIIMOTE_TILT_LEFT": ("motion", MOT_WIIMOTE_TILT_LEFT),
    "motion.WIIMOTE_TILT_RIGHT": ("motion", MOT_WIIMOTE_TILT_RIGHT),
    "motion.WIIMOTE_TILT_UP": ("motion", MOT_WIIMOTE_TILT_UP),
    "motion.WIIMOTE_TILT_DOWN": ("motion", MOT_WIIMOTE_TILT_DOWN),
    "motion.NUNCHUK_SHAKE": ("motion", MOT_NUNCHUK_SHAKE),
    "motion.NUNCHUK_TILT_LEFT": ("motion", MOT_NUNCHUK_TILT_LEFT),
    "motion.NUNCHUK_TILT_RIGHT": ("motion", MOT_NUNCHUK_TILT_RIGHT),
    "motion.NUNCHUK_TILT_UP": ("motion", MOT_NUNCHUK_TILT_UP),
    "motion.NUNCHUK_TILT_DOWN": ("motion", MOT_NUNCHUK_TILT_DOWN),
}

SOURCE_LABELS = {
    "wiimote.UP": "D-Pad Up",
    "wiimote.DOWN": "D-Pad Down",
    "wiimote.LEFT": "D-Pad Left",
    "wiimote.RIGHT": "D-Pad Right",
    "wiimote.A": "A",
    "wiimote.B": "B (trigger)",
    "wiimote.ONE": "1",
    "wiimote.TWO": "2",
    "wiimote.PLUS": "Plus (+)",
    "wiimote.MINUS": "Minus (-)",
    "wiimote.HOME": "Home",
    "nunchuk.C": "C",
    "nunchuk.Z": "Z",
    "classic.UP": "D-Pad Up",
    "classic.DOWN": "D-Pad Down",
    "classic.LEFT": "D-Pad Left",
    "classic.RIGHT": "D-Pad Right",
    "classic.A": "a",
    "classic.B": "b",
    "classic.X": "x",
    "classic.Y": "y",
    "classic.L": "L (full press)",
    "classic.R": "R (full press)",
    "classic.ZL": "ZL",
    "classic.ZR": "ZR",
    "classic.PLUS": "Plus (+)",
    "classic.MINUS": "Minus (-)",
    "classic.HOME": "Home",
    "motion.WIIMOTE_SHAKE": "Wiimote: shake",
    "motion.WIIMOTE_TILT_LEFT": "Wiimote: tilt left",
    "motion.WIIMOTE_TILT_RIGHT": "Wiimote: tilt right",
    "motion.WIIMOTE_TILT_UP": "Wiimote: tilt up (point up)",
    "motion.WIIMOTE_TILT_DOWN": "Wiimote: tilt down (point down)",
    "motion.NUNCHUK_SHAKE": "Nunchuk: shake",
    "motion.NUNCHUK_TILT_LEFT": "Nunchuk: tilt left",
    "motion.NUNCHUK_TILT_RIGHT": "Nunchuk: tilt right",
    "motion.NUNCHUK_TILT_UP": "Nunchuk: tilt up",
    "motion.NUNCHUK_TILT_DOWN": "Nunchuk: tilt down",
}

STICK_SOURCES = {
    "none": "None",
    "nunchuk.stick": "Nunchuk stick",
    "classic.left_stick": "Classic left stick",
    "classic.right_stick": "Classic right stick",
    "wiimote.tilt": "Wiimote tilt (accelerometer)",
    "nunchuk.tilt": "Nunchuk tilt (accelerometer)",
    "ir.pointer": "IR pointer (needs sensor bar)",
    "wiimote.dpad": "Wiimote D-Pad",
}

TRIGGER_SOURCES = {
    "none": "None (digital only)",
    "classic.lt": "Classic L (analog)",
    "classic.rt": "Classic R (analog)",
}

ORIENTATIONS = {
    "vertical": "Vertical (pointing at screen)",
    "sideways": "Sideways (NES style, D-Pad on the left)",
}

# --------------------------------------------------------------------------
# Keyboard & mouse targets (Windows virtual-key codes)
# --------------------------------------------------------------------------

MOUSE_BUTTON_TARGETS = {
    "MOUSE_LEFT": "Mouse: Left click",
    "MOUSE_RIGHT": "Mouse: Right click",
    "MOUSE_MIDDLE": "Mouse: Middle click",
    "MOUSE_X1": "Mouse: Back (X1)",
    "MOUSE_X2": "Mouse: Forward (X2)",
    "WHEEL_UP": "Mouse: Wheel up",
    "WHEEL_DOWN": "Mouse: Wheel down",
}


def _build_key_targets() -> dict[str, tuple[int, str]]:
    keys: dict[str, tuple[int, str]] = {}
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        keys[c] = (ord(c), c)
    for d in "0123456789":
        keys[d] = (ord(d), d)
    for n in range(1, 13):
        keys[f"F{n}"] = (0x6F + n, f"F{n}")
    named = [
        ("SPACE", 0x20, "Space"), ("ENTER", 0x0D, "Enter"), ("ESCAPE", 0x1B, "Esc"),
        ("TAB", 0x09, "Tab"), ("BACKSPACE", 0x08, "Backspace"),
        ("LSHIFT", 0xA0, "Left Shift"), ("RSHIFT", 0xA1, "Right Shift"),
        ("LCTRL", 0xA2, "Left Ctrl"), ("RCTRL", 0xA3, "Right Ctrl"),
        ("LALT", 0xA4, "Left Alt"), ("RALT", 0xA5, "Right Alt"), ("LWIN", 0x5B, "Windows key"),
        ("UP", 0x26, "Arrow Up"), ("DOWN", 0x28, "Arrow Down"),
        ("LEFT", 0x25, "Arrow Left"), ("RIGHT", 0x27, "Arrow Right"),
        ("INSERT", 0x2D, "Insert"), ("DELETE", 0x2E, "Delete"), ("HOME", 0x24, "Home"),
        ("END", 0x23, "End"), ("PAGEUP", 0x21, "Page Up"), ("PAGEDOWN", 0x22, "Page Down"),
        ("CAPSLOCK", 0x14, "Caps Lock"), ("MINUS", 0xBD, "- (minus)"), ("EQUALS", 0xBB, "= (equals)"),
        ("COMMA", 0xBC, ", (comma)"), ("PERIOD", 0xBE, ". (period)"), ("SLASH", 0xBF, "/ (slash)"),
        ("SEMICOLON", 0xBA, "; (semicolon)"), ("APOSTROPHE", 0xDE, "' (apostrophe)"),
        ("LBRACKET", 0xDB, "[ (left bracket)"), ("RBRACKET", 0xDD, "] (right bracket)"),
        ("BACKSLASH", 0xDC, "\\ (backslash)"), ("GRAVE", 0xC0, "` (grave / console)"),
        ("NUMPAD0", 0x60, "Numpad 0"), ("NUMPAD1", 0x61, "Numpad 1"), ("NUMPAD2", 0x62, "Numpad 2"),
        ("NUMPAD3", 0x63, "Numpad 3"), ("NUMPAD4", 0x64, "Numpad 4"), ("NUMPAD5", 0x65, "Numpad 5"),
        ("NUMPAD6", 0x66, "Numpad 6"), ("NUMPAD7", 0x67, "Numpad 7"), ("NUMPAD8", 0x68, "Numpad 8"),
        ("NUMPAD9", 0x69, "Numpad 9"),
        ("VOLUME_UP", 0xAF, "Volume up"), ("VOLUME_DOWN", 0xAE, "Volume down"),
        ("VOLUME_MUTE", 0xAD, "Volume mute"), ("MEDIA_PLAY_PAUSE", 0xB3, "Media play/pause"),
        ("MEDIA_NEXT", 0xB0, "Media next track"), ("MEDIA_PREV", 0xB1, "Media previous track"),
    ]
    for name, vk, label in named:
        keys[name] = (vk, label)
    return keys


# name -> (virtual-key code, label)
KEY_TARGETS: dict[str, tuple[int, str]] = _build_key_targets()

# Keys that need KEYEVENTF_EXTENDEDKEY when sent as scan codes
EXTENDED_VKS = frozenset({
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E,  # nav cluster + arrows
    0xA3, 0xA5, 0x5B,  # right ctrl, right alt, windows key
    0xAD, 0xAE, 0xAF, 0xB0, 0xB1, 0xB3,  # media / volume
})

# Combined keyboard/mouse target labels, in UI order ("NONE" first)
KBM_TARGET_LABELS: dict[str, str] = {"NONE": "(none)"}
KBM_TARGET_LABELS.update(MOUSE_BUTTON_TARGETS)
KBM_TARGET_LABELS.update({name: f"Key: {label}" for name, (_vk, label) in KEY_TARGETS.items()})

MOUSE_MODES = {
    "relative": "Relative (stick/tilt moves the cursor; best for games)",
    "absolute": "Absolute (IR pointer = cursor position; best for desktop)",
}

KEY_STICK_LAYOUTS = {
    "wasd": "W A S D",
    "arrows": "Arrow keys",
}
