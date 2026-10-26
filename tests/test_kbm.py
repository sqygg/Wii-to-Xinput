from wiimote_xinput.config import KeyStickConfig, MouseConfig, Profile
from wiimote_xinput.constants import BTN_A, BTN_B, BTN_DOWN, BTN_UP, NUN_C
from wiimote_xinput.kbm_output import KbmOutputFactory, RecordingBackend
from wiimote_xinput.mapper import NEUTRAL, KbmState, Mapper
from wiimote_xinput.parser import NunchukState, WiimoteState


def kbm_profile(**kw) -> Profile:
    p = Profile(name="kbm", xinput_enabled=False, **kw)
    p.keys = {"wiimote.B": "MOUSE_LEFT", "wiimote.A": "SPACE", "wiimote.UP": "WHEEL_UP", "nunchuk.C": "LSHIFT"}
    return p


# -- mapper ----------------------------------------------------------------


def test_key_bindings_and_xinput_disabled():
    out, kbm = Mapper(kbm_profile()).map_all(WiimoteState(buttons=BTN_A | BTN_B))
    assert out == NEUTRAL
    assert kbm.keys == {"SPACE", "MOUSE_LEFT"}


def test_keys_work_alongside_xinput():
    p = Profile()
    p.keys = {"wiimote.A": "E"}
    out, kbm = Mapper(p).map_all(WiimoteState(buttons=BTN_A))
    assert out.buttons != 0 and kbm.keys == {"E"}


def test_key_stick_wasd_and_arrows():
    p = kbm_profile()
    p.key_stick = KeyStickConfig(source="nunchuk.stick", layout="wasd", threshold=0.5)
    m = Mapper(p)
    _, kbm = m.map_all(WiimoteState(nunchuk=NunchukState(stick=(-0.8, 0.9), buttons=NUN_C)))
    assert kbm.keys == {"W", "A", "LSHIFT"}
    _, kbm = m.map_all(WiimoteState(nunchuk=NunchukState(stick=(0.3, -0.3))))
    assert kbm.keys == frozenset()
    p.key_stick.layout = "arrows"
    m.set_profile(p)
    _, kbm = m.map_all(WiimoteState(nunchuk=NunchukState(stick=(0.7, -0.7))))
    assert kbm.keys == {"RIGHT", "DOWN"}


def test_mouse_relative_and_absolute():
    p = kbm_profile()
    p.mouse = MouseConfig(source="nunchuk.stick", deadzone=0.0, curve=1.0)
    _, kbm = Mapper(p).map_all(WiimoteState(nunchuk=NunchukState(stick=(1.0, 0.0))))
    assert kbm.mouse_velocity == (1.0, 0.0) and kbm.mouse_absolute is None

    p.mouse = MouseConfig(source="ir.pointer", mode="absolute", absolute_range=0.5)
    m = Mapper(p)
    _, kbm = m.map_all(WiimoteState(pointer=(0.25, -0.75)))
    assert kbm.mouse_absolute == (0.5, -1.0)
    _, kbm = m.map_all(WiimoteState(pointer=None))  # sensor bar out of view
    assert kbm.mouse_absolute is None and kbm.mouse_velocity == (0.0, 0.0)


def test_needs_ir_for_mouse():
    p = Profile()
    assert not p.needs_ir()
    p.mouse.source = "ir.pointer"
    assert p.needs_ir()


def test_sideways_rotation_applies_to_keys():
    p = kbm_profile(orientation="sideways")
    p.keys = {"wiimote.LEFT": "A"}
    _, kbm = Mapper(p).map_all(WiimoteState(buttons=BTN_UP))  # physical up = logical left
    assert kbm.keys == {"A"}
    _, kbm = Mapper(p).map_all(WiimoteState(buttons=BTN_DOWN))
    assert kbm.keys == frozenset()


# -- output ----------------------------------------------------------------


def make_output():
    backend = RecordingBackend()
    factory = KbmOutputFactory(backend)
    return backend, factory


def test_press_release_diffing():
    backend, factory = make_output()
    out = factory()
    cfg = MouseConfig()
    out.apply(KbmState(keys=frozenset({"W", "MOUSE_LEFT"})), cfg, now=0.0)
    out.apply(KbmState(keys=frozenset({"W", "MOUSE_LEFT"})), cfg, now=0.01)  # no repeat events
    out.apply(KbmState(keys=frozenset({"W"})), cfg, now=0.02)
    out.release_all()
    events = list(backend.events)
    assert set(events[:2]) == {("key", "W", True), ("button", "MOUSE_LEFT", True)}
    assert events[2:] == [("button", "MOUSE_LEFT", False), ("key", "W", False)]
    downs = [e for e in backend.events if e[2] is True]
    ups = [e for e in backend.events if e[2] is False]
    assert len(downs) == 2 and len(ups) == 2


def test_shared_key_held_by_two_remotes():
    backend, factory = make_output()
    a, b = factory(), factory()
    cfg = MouseConfig()
    held = KbmState(keys=frozenset({"SPACE"}))
    a.apply(held, cfg, now=0.0)
    b.apply(held, cfg, now=0.0)
    a.apply(KbmState(), cfg, now=0.1)
    assert list(backend.events) == [("key", "SPACE", True)]  # still held by remote b
    b.release_all()
    assert list(backend.events)[-1] == ("key", "SPACE", False)


def test_wheel_repeats_while_held():
    backend, factory = make_output()
    out = factory()
    cfg = MouseConfig()
    up = KbmState(keys=frozenset({"WHEEL_UP"}))
    for t in (0.0, 0.05, 0.13, 0.20, 0.26):
        out.apply(up, cfg, now=t)
    assert [e for e in backend.events if e[0] == "wheel"] == [("wheel", 1)] * 3
    out.apply(KbmState(), cfg, now=0.3)
    out.apply(up, cfg, now=0.31)  # new press scrolls immediately
    assert len([e for e in backend.events if e[0] == "wheel"]) == 4


def test_relative_motion_integrates_with_subpixel_carry():
    backend, factory = make_output()
    out = factory()
    cfg = MouseConfig(speed=100.0)  # 100 px/s -> 1 px per 10 ms report at full deflection
    t = 0.0
    out.apply(KbmState(mouse_velocity=(0.25, 0.5)), cfg, now=t)
    for _ in range(100):  # one second at 100 Hz
        t += 0.01
        out.apply(KbmState(mouse_velocity=(0.25, 0.5)), cfg, now=t)
    x, y = backend.cursor
    assert 24 <= x <= 25 and -50 <= y <= -49  # +Y stick = up = negative screen dy


def test_absolute_pointer_smoothing():
    backend, factory = make_output()
    out = factory()
    cfg = MouseConfig(smoothing=0.5)
    out.apply(KbmState(mouse_absolute=(0.0, 0.0)), cfg, now=0.0)
    out.apply(KbmState(mouse_absolute=(1.0, 1.0)), cfg, now=0.01)
    assert list(backend.events) == [("abs", 0.5, 0.5), ("abs", 0.75, 0.25)]
