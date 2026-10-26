from wiimote_xinput.config import Profile, StickConfig
from wiimote_xinput.constants import (
    BTN_A, BTN_B, BTN_HOME, BTN_LEFT, BTN_ONE, BTN_RIGHT, BTN_TWO, BTN_UP, CC_B, CC_L, NUN_C, NUN_Z,
    XUSB_A, XUSB_B, XUSB_DPAD_DOWN, XUSB_DPAD_LEFT, XUSB_DPAD_UP, XUSB_GUIDE, XUSB_LEFT_SHOULDER,
    XUSB_RIGHT_SHOULDER, XUSB_X, XUSB_Y,
)
from wiimote_xinput.mapper import Mapper, apply_stick_response, rotate_dpad_sideways
from wiimote_xinput.parser import ClassicState, NunchukState, WiimoteState


def test_default_buttons():
    m = Mapper(Profile())
    out = m.map(WiimoteState(buttons=BTN_A | BTN_B | BTN_ONE | BTN_TWO | BTN_HOME | BTN_UP))
    assert out.buttons == XUSB_A | XUSB_B | XUSB_X | XUSB_Y | XUSB_GUIDE | XUSB_DPAD_UP


def test_nunchuk_stick_and_bumpers():
    m = Mapper(Profile())
    out = m.map(WiimoteState(nunchuk=NunchukState(stick=(1.0, 0.0), buttons=NUN_C | NUN_Z)))
    assert out.buttons == XUSB_LEFT_SHOULDER | XUSB_RIGHT_SHOULDER
    assert out.lx == 32767 and out.ly == 0


def test_c_z_as_triggers():
    p = Profile()
    p.buttons["nunchuk.C"] = "LT"
    p.buttons["nunchuk.Z"] = "RT"
    out = Mapper(p).map(WiimoteState(nunchuk=NunchukState(buttons=NUN_Z)))
    assert out.rt == 255 and out.lt == 0 and out.buttons == 0


def test_deadzone_and_inversion():
    cfg = StickConfig(deadzone=0.2, saturation=1.0)
    assert apply_stick_response(0.1, 0.1, cfg) == (0.0, 0.0)
    x, y = apply_stick_response(0.6, 0.0, cfg)
    assert abs(x - 0.5) < 1e-9 and y == 0.0
    x, y = apply_stick_response(0.0, 1.0, StickConfig(deadzone=0.0, invert_y=True))
    assert y == -1.0
    # Saturation: 0.9 deflection already counts as full.
    x, _ = apply_stick_response(0.9, 0.0, StickConfig(deadzone=0.0, saturation=0.9))
    assert x == 1.0


def test_sideways_rotation():
    assert rotate_dpad_sideways(BTN_UP) == BTN_LEFT
    assert rotate_dpad_sideways(BTN_RIGHT) == BTN_UP
    p = Profile(orientation="sideways")
    out = Mapper(p).map(WiimoteState(buttons=BTN_UP | BTN_LEFT))
    assert out.buttons == XUSB_DPAD_LEFT | XUSB_DPAD_DOWN


def test_classic_controller_defaults():
    m = Mapper(Profile())
    cs = ClassicState(left=(0.0, 1.0), right=(1.0, 0.0), lt=0.5, rt=0.0, buttons=CC_B | CC_L)
    out = m.map(WiimoteState(classic=cs))
    assert out.buttons & XUSB_A  # Nintendo b (bottom) -> Xbox A (bottom)
    assert out.lt == 255  # L full press bound to LT digitally wins over analog
    assert out.ly == 32767  # classic left stick fallback
    assert out.rx == 32767


def test_profile_hot_swap():
    m = Mapper(Profile())
    p = Profile()
    p.buttons["wiimote.A"] = "Y"
    m.set_profile(p)
    assert m.map(WiimoteState(buttons=BTN_A)).buttons == XUSB_Y


def test_dpad_as_stick():
    p = Profile()
    p.left_stick = StickConfig(source="wiimote.dpad", deadzone=0.0)
    out = Mapper(p).map(WiimoteState(buttons=BTN_RIGHT))
    assert out.lx == 32767


def test_tilt_source():
    p = Profile()
    p.left_stick = StickConfig(source="wiimote.tilt", deadzone=0.0, tilt_range_deg=45)
    m = Mapper(p)
    assert m.map(WiimoteState(accel=(0.0, 0.0, 1.0))).lx == 0
    assert m.map(WiimoteState(accel=(1.0, 0.0, 1.0))).lx == 32767  # 45 degrees roll
