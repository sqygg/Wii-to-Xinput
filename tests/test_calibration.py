import random

import pytest

from wiimote_xinput.calibration import (
    AccelCalibrator, CalibrationError, ClassicCalibrator, DeadzoneCalibrator, DeviceCalibration,
    IrCalibrator, NunchukStickCalibrator, RawSnapshot, apply_overrides,
)
from wiimote_xinput.config import ConfigStore, Profile
from wiimote_xinput.parser import (
    AccelCalibration, ClassicCalibration, ClassicRaw, IrCalibration, NunchukCalibration,
    NunchukRaw, NunchukState, WiimoteState, parse_classic,
)
from wiimote_xinput.simulator import encode_classic

rng = random.Random(1)
FACTORY = AccelCalibration(zero=(512, 512, 512), one_g=(616, 616, 616))


def run(cal, samples_per_step):
    """Drive a calibrator through all steps; samples_per_step: list of lists of RawSnapshot."""
    done = False
    for samples in samples_per_step:
        cal.start_step()
        for raw in samples:
            cal.feed(raw)
        done = cal.finish_step()
    return done


def accel_samples(mean, n=20, noise=1):
    return [RawSnapshot(accel=tuple(int(m + rng.randint(-noise, noise)) for m in mean)) for _ in range(n)]


# True device: zero (500, 520, 508), one-g gain (100, 110, 95)
FLAT = (500, 520, 508 + 95)
UPRIGHT = (500, 520 + 110, 508)
SIDE = (500 - 100, 520, 508)  # lying on the other side: gravity reads negative


def test_accel_three_pose_calibration():
    c = AccelCalibrator("wiimote", FACTORY)
    assert run(c, [accel_samples(FLAT), accel_samples(UPRIGHT), accel_samples(SIDE)])
    assert c.result.zero == (500, 520, 508)
    assert c.result.one_g == (600, 630, 603)
    new = c.apply(DeviceCalibration())
    assert new.accel == c.result and c.reset(new).accel is None


def test_accel_wrong_pose_and_movement_rejected():
    c = AccelCalibrator("wiimote", FACTORY)
    c.start_step()
    for r in accel_samples(UPRIGHT):  # asked for flat
        c.feed(r)
    with pytest.raises(CalibrationError, match="position"):
        c.finish_step()
    assert c.step == 0  # stays on the same step
    c.start_step()
    for r in accel_samples(FLAT, noise=30):
        c.feed(r)
    with pytest.raises(CalibrationError, match="moved"):
        c.finish_step()


def test_nunchuk_accel_needs_nunchuk():
    c = AccelCalibrator("nunchuk", FACTORY)
    assert "Nunchuk" in c.unavailable_reason(RawSnapshot(accel=(1, 2, 3)))


def nun(stick):
    return RawSnapshot(nunchuk=NunchukRaw(stick, (512, 512, 616), 0))


def test_nunchuk_stick_center_and_range():
    c = NunchukStickCalibrator()
    rest = [nun((130 + rng.randint(-1, 1), 125)) for _ in range(20)]
    circle = [nun(p) for p in ((40, 125), (220, 125), (130, 30), (130, 215), (60, 60))]
    assert run(c, [rest, circle])
    assert c.result == ((40, 130, 220), (30, 125, 215))


def test_nunchuk_stick_range_too_small():
    c = NunchukStickCalibrator()
    run(c, [[nun((128, 128))] * 20])
    c.start_step()
    for p in ((110, 128), (150, 128), (128, 100), (128, 150)):
        c.feed(nun(p))
    with pytest.raises(CalibrationError, match="X axis"):
        c.finish_step()


def classic(left, right, lt=0, rt=0):
    return RawSnapshot(classic=ClassicRaw(left, right, lt, rt, 0))


def test_classic_sticks_and_triggers():
    c = ClassicCalibrator()
    rest = [classic((128, 132), (128, 128), lt=2, rt=1)] * 20
    moves = [classic((20, 30), (16, 24), 29, 0), classic((236, 228), (240, 232), 0, 28), classic((128, 132), (128, 128))]
    assert run(c, [rest, moves])
    left, right, triggers = c.result
    assert left == ((20, 128, 236), (30, 132, 228))
    assert triggers == ((2, 29), (1, 28))
    applied = apply_overrides(FACTORY, NunchukCalibration(), ClassicCalibration(), c.apply(DeviceCalibration()))
    st = parse_classic(encode_classic(0, 0, 0, 0, lt=29, rt=1, buttons=0), applied.classic)
    assert st.lt == 1.0 and st.rt == 0.0


def test_classic_without_analog_triggers_keeps_default():
    c = ClassicCalibrator()
    run(c, [[classic((128, 128), (128, 128))] * 20,
            [classic((20, 20), (20, 20)), classic((236, 236), (236, 236))]])
    assert c.result[2] is None and c.triggers_kept_default
    assert c.apply(DeviceCalibration()).classic_triggers is None


def ptr(p):
    return RawSnapshot(pointer_raw=p)


def test_ir_calibration_maps_corners():
    c = IrCalibrator()
    assert run(c, [[ptr((0.1, -0.2))] * 15, [ptr((-0.3, 0.1))] * 15, [ptr((0.5, -0.5))] * 15])
    ir = c.result
    assert ir.apply((0.1, -0.2)) == pytest.approx((0.0, 0.0))
    assert ir.apply((-0.3, 0.1)) == pytest.approx((-1.0, 1.0))
    assert ir.apply((0.5, -0.5)) == pytest.approx((1.0, -1.0))


def test_ir_calibration_needs_visible_bar_and_spread():
    c = IrCalibrator()
    c.start_step()
    for p in [None] * 10 + [(0.0, 0.0)] * 5:
        c.feed(ptr(p))
    with pytest.raises(CalibrationError, match="sensor bar"):
        c.finish_step()
    c = IrCalibrator()
    with pytest.raises(CalibrationError, match="too close"):
        run(c, [[ptr((0.0, 0.0))] * 15, [ptr((-0.02, 0.02))] * 15, [ptr((0.02, -0.02))] * 15])
    # Retrying the last step works after the error
    c.start_step()
    for _ in range(15):
        c.feed(ptr((0.4, -0.4)))
    assert c.finish_step() and c.result is not None


def test_deadzone_helper_suggests_and_applies():
    c = DeadzoneCalibrator()
    samples = [RawSnapshot(state=WiimoteState(nunchuk=NunchukState(stick=(0.06, 0.02)))) for _ in range(20)]
    assert run(c, [samples])
    dz = c.suggestions["nunchuk.stick"]
    assert 0.09 <= dz <= 0.12
    p = Profile()
    p.mouse = p.mouse.__class__(source="nunchuk.stick")
    changes = c.apply_to_profile(p)
    assert p.left_stick.deadzone == dz and p.mouse.deadzone == dz and len(changes) == 2
    held = [RawSnapshot(state=WiimoteState(nunchuk=NunchukState(stick=(0.9, 0.0))))] * 20
    with pytest.raises(CalibrationError, match="held"):
        run(DeadzoneCalibrator(), [held])


def test_overrides_do_not_touch_factory():
    factory_n = NunchukCalibration()
    user = DeviceCalibration(nunchuk_stick=((40, 130, 220), (30, 125, 215)))
    eff = apply_overrides(FACTORY, factory_n, ClassicCalibration(), user)
    assert eff.nunchuk.stick_x.center == 130 and not eff.nunchuk.stick_x.learn
    assert factory_n.stick_x.center == 128
    assert eff.accel is FACTORY and eff.ir.apply((0.5, 0.5)) == (0.5, 0.5)


def test_device_calibration_roundtrip_and_validation(tmp_path):
    cal = DeviceCalibration(
        accel=AccelCalibration((500, 520, 508), (600, 630, 603)),
        nunchuk_stick=((40, 130, 220), (30, 125, 215)),
        classic_triggers=((2, 29), (1, 28)),
        ir=IrCalibration((0.1, -0.2), (0.4, 0.3)),
    )
    assert DeviceCalibration.from_dict(cal.to_dict()) == cal
    bad = DeviceCalibration.from_dict({
        "accel": {"zero": [500, 520], "one_g": [1, 2, 3]},
        "nunchuk_stick": {"x": [200, 100, 50], "y": [1, 2, 3]},
        "ir": {"center": [0, 0], "half_range": [0, 1]},
        "classic_triggers": "nope",
    })
    assert bad.is_empty()

    store = ConfigStore(tmp_path / "c.json").load()
    store.set_calibration("AA:BB", cal)
    assert ConfigStore(tmp_path / "c.json").load().get_calibration("AA:BB") == cal
    store.set_calibration("AA:BB", None)
    assert ConfigStore(tmp_path / "c.json").load().get_calibration("AA:BB") is None
