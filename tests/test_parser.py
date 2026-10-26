from wiimote_xinput.constants import (
    BTN_A, BTN_HOME, BTN_PLUS, BTN_TWO, BTN_UP, CC_A, CC_HOME, CC_UP, CC_ZR,
    ExtensionType, identify_extension,
)
from wiimote_xinput.parser import (
    AccelCalibration, ClassicCalibration, DataReport, MemoryReadResponse, NunchukCalibration,
    StatusReport, Acknowledge, ir_pointer, parse_classic, parse_input_report, parse_ir, parse_nunchuk,
)
from wiimote_xinput.simulator import (
    _eeprom_accel_block, encode_accel, encode_buttons, encode_classic, encode_nunchuk,
    nunchuk_calibration_block,
)


def test_status_report():
    rep = parse_input_report(bytes([0x20, 0x00, 0x08, 0x12, 0, 0, 0x64]))
    assert isinstance(rep, StatusReport)
    assert rep.buttons == BTN_A
    assert rep.extension_connected and not rep.battery_low
    assert rep.leds == 0x10
    assert rep.battery_percent == 50


def test_memory_read_and_ack():
    rep = parse_input_report(bytes([0x21, 0, 0, 0x50, 0x00, 0xFA]) + bytes([0, 0, 0xA4, 0x20, 0, 0]) + bytes(10))
    assert isinstance(rep, MemoryReadResponse)
    assert rep.size == 6 and rep.error == 0 and rep.address == 0x00FA
    assert identify_extension(rep.data) == ExtensionType.NUNCHUK
    ack = parse_input_report(bytes([0x22, 0, 0, 0x16, 0x03]))
    assert isinstance(ack, Acknowledge) and ack.report == 0x16 and ack.error == 3


def test_identify_extension():
    assert identify_extension(bytes([0xFF, 0x00, 0xA4, 0x20, 0x00, 0x00])) == ExtensionType.NUNCHUK
    assert identify_extension(bytes([0x00, 0x00, 0xA4, 0x20, 0x01, 0x01])) == ExtensionType.CLASSIC
    assert identify_extension(bytes([0x01, 0x00, 0xA4, 0x20, 0x01, 0x01])) == ExtensionType.CLASSIC_PRO
    assert identify_extension(b"\xff" * 6) == ExtensionType.PARTIAL
    assert identify_extension(bytes([0, 0, 0xA4, 0x20, 0x04, 0x05])) == ExtensionType.MOTION_PLUS


def test_buttons_and_accel_roundtrip():
    accel = (601, 514, 618)  # Y/Z only have 9 significant bits
    buttons = BTN_UP | BTN_A | BTN_TWO | BTN_HOME | BTN_PLUS
    payload = encode_buttons(buttons, accel) + encode_accel(accel)
    rep = parse_input_report(bytes([0x31]) + payload)
    assert isinstance(rep, DataReport)
    assert rep.buttons == buttons
    assert rep.accel_raw == (601, 514, 618)


def test_report_too_short_is_ignored():
    assert parse_input_report(bytes([0x35, 0, 0, 1, 2])) is None
    assert parse_input_report(b"") is None
    assert parse_input_report(bytes([0x99, 1, 2, 3])) is None


def test_accel_calibration_from_eeprom():
    cal = AccelCalibration.from_eeprom(_eeprom_accel_block(0x80, 0x9A))
    assert cal is not None
    assert cal.zero == (512, 512, 512) and cal.one_g == (616, 616, 616)
    gx, gy, gz = cal.normalize((512, 512, 616))
    assert abs(gz - 1.0) < 1e-9 and gx == 0 and gy == 0
    bad = bytearray(_eeprom_accel_block())
    bad[9] ^= 0xFF
    assert AccelCalibration.from_eeprom(bad) is None


def test_nunchuk_parse_with_calibration():
    cal = NunchukCalibration.from_bytes(nunchuk_calibration_block())
    assert (cal.stick_x.minimum, cal.stick_x.center, cal.stick_x.maximum) == (0x20, 0x80, 0xE0)
    st = parse_nunchuk(encode_nunchuk(0xE0, 0x80, (512, 512, 716), c=True, z=False), cal)
    assert st.stick == (1.0, 0.0)
    assert st.c and not st.z
    assert abs(st.accel[2] - 1.0) < 0.01
    st = parse_nunchuk(encode_nunchuk(0x20, 0x20, (512, 512, 512), c=False, z=True), cal)
    assert st.stick == (-1.0, -1.0) and st.z and not st.c


def test_nunchuk_bad_calibration_falls_back_and_learns():
    cal = NunchukCalibration.from_bytes(b"\xff" * 16)
    assert cal.stick_x.center == 128
    st = parse_nunchuk(encode_nunchuk(250, 128, (512, 512, 512), False, False), cal)
    assert st.stick[0] == 1.0
    assert cal.stick_x.maximum == 250  # range learned


def test_nunchuk_unplugged_bytes():
    assert parse_nunchuk(b"\xff" * 6, NunchukCalibration()) is None


def test_classic_parse():
    data = encode_classic(lx=63, ly=0, rx=16, ry=31, lt=31, rt=0, buttons=CC_A | CC_UP | CC_ZR | CC_HOME)
    st = parse_classic(data, ClassicCalibration())
    assert st.buttons == CC_A | CC_UP | CC_ZR | CC_HOME
    assert st.left[0] == 1.0 and st.left[1] == -1.0
    assert abs(st.right[0]) < 0.05 and st.right[1] == 1.0
    assert st.lt == 1.0 and st.rt == 0.0


def test_ir_basic_and_pointer():
    # Two dots at (400, 384) and (600, 384), second pair invalid.
    x1, x2, y = 400, 600, 384
    b2 = ((y >> 8) << 6) | ((x1 >> 8) << 4) | ((y >> 8) << 2) | (x2 >> 8)
    data = bytes([x1 & 0xFF, y & 0xFF, b2, x2 & 0xFF, y & 0xFF]) + b"\xff" * 5
    dots = parse_ir(data)
    assert [(d.x, d.y) for d in dots] == [(400, 384), (600, 384)]
    px, py = ir_pointer(dots)
    assert abs(px - (1 - 2 * 500 / 1023)) < 1e-9
    assert abs(py - (2 * 384 / 767 - 1)) < 1e-9
    assert ir_pointer([]) is None
