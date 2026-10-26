import math

from wiimote_xinput.config import MotionConfig, Profile
from wiimote_xinput.constants import (
    MOT_NUNCHUK_SHAKE, MOT_NUNCHUK_TILT_UP, MOT_WIIMOTE_SHAKE, MOT_WIIMOTE_TILT_DOWN,
    MOT_WIIMOTE_TILT_LEFT, MOT_WIIMOTE_TILT_RIGHT, MOT_WIIMOTE_TILT_UP, XUSB_Y,
)
from wiimote_xinput.mapper import Mapper
from wiimote_xinput.motion import MotionDetector
from wiimote_xinput.parser import WiimoteState

CFG = MotionConfig(shake_threshold_g=1.0, shake_hold_s=0.2, tilt_angle_deg=30.0)
FLAT = (0.0, 0.0, 1.0)


def roll(deg):  # vertical remote rolled to the right by deg
    r = math.radians(deg)
    return (math.sin(r), 0.0, math.cos(r))


def test_flat_remote_has_no_gestures():
    assert MotionDetector().update(FLAT, FLAT, False, CFG, now=0.0) == 0


def test_shake_press_hold_and_release():
    d = MotionDetector()
    assert d.update((0.0, 2.5, 1.0), None, False, CFG, now=0.0) & MOT_WIIMOTE_SHAKE
    assert d.update(FLAT, None, False, CFG, now=0.15) & MOT_WIIMOTE_SHAKE  # still held
    assert not d.update(FLAT, None, False, CFG, now=0.25) & MOT_WIIMOTE_SHAKE
    # Free fall (|a| near 0) is a shake too
    assert d.update((0.0, 0.0, 0.0), None, False, CFG, now=1.0) & MOT_WIIMOTE_SHAKE


def test_tilt_directions_with_hysteresis():
    d = MotionDetector()
    assert d.update(roll(25), None, False, CFG, now=0) == 0
    assert d.update(roll(32), None, False, CFG, now=0) == MOT_WIIMOTE_TILT_RIGHT
    assert d.update(roll(27), None, False, CFG, now=0) == MOT_WIIMOTE_TILT_RIGHT  # above release angle
    assert d.update(roll(24), None, False, CFG, now=0) == 0
    assert d.update(roll(-40), None, False, CFG, now=0) == MOT_WIIMOTE_TILT_LEFT
    up = (0.0, math.sin(math.radians(45)), math.cos(math.radians(45)))
    assert d.update(up, None, False, CFG, now=0) == MOT_WIIMOTE_TILT_UP
    down = (0.0, -up[1], up[2])
    assert d.update(down, None, False, CFG, now=0) == MOT_WIIMOTE_TILT_DOWN


def test_tilt_frozen_while_shaking():
    d = MotionDetector()
    d.update(roll(0), None, False, CFG, now=0.0)
    bits = d.update((2.0, 0.0, 1.0), None, False, CFG, now=0.1)  # violent swing to the right
    assert bits == MOT_WIIMOTE_SHAKE  # no spurious tilt-right


def test_sideways_steering():
    d = MotionDetector()
    # Held sideways: gravity mostly along X; tilting the "wheel" right raises +Y
    r = math.radians(40)
    assert d.update((math.cos(r), math.sin(r), 0.0), None, True, CFG, now=0) == MOT_WIIMOTE_TILT_RIGHT


def test_nunchuk_gestures_and_disable():
    d = MotionDetector()
    up = (0.0, math.sin(math.radians(50)), math.cos(math.radians(50)))
    assert d.update(FLAT, up, False, CFG, now=0) == MOT_NUNCHUK_TILT_UP
    assert d.update(FLAT, (0.0, 0.0, 3.0), False, CFG, now=1) & MOT_NUNCHUK_SHAKE
    off = MotionConfig(enabled=False)
    assert d.update((0.0, 3.0, 0.0), (0.0, 3.0, 0.0), False, off, now=2) == 0


def test_gestures_bind_to_xbox_and_keys():
    p = Profile()
    p.buttons["motion.WIIMOTE_SHAKE"] = "Y"
    p.keys["motion.WIIMOTE_TILT_LEFT"] = "Q"
    out, kbm = Mapper(p).map_all(WiimoteState(motion=MOT_WIIMOTE_SHAKE | MOT_WIIMOTE_TILT_LEFT))
    assert out.buttons & XUSB_Y
    assert kbm.keys == {"Q"}


def test_motion_config_sanitized():
    p = Profile.from_dict({"motion": {"enabled": "no", "shake_threshold_g": 99, "tilt_angle_deg": 1},
                           "buttons": {"motion.NUNCHUK_SHAKE": "A"}})
    assert p.motion.enabled is True and p.motion.shake_threshold_g == 2.5 and p.motion.tilt_angle_deg == 10.0
    assert p.buttons == {"motion.NUNCHUK_SHAKE": "A"}
