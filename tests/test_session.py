"""End-to-end: session thread + protocol simulator + dry-run virtual pad."""

import time

from wiimote_xinput.bluetooth_core import ConnectionStatus, DeviceManager
from wiimote_xinput.config import ConfigStore
from wiimote_xinput.constants import ExtensionType
from wiimote_xinput.simulator import SIMULATED_INFO, SimulatedTransport
from wiimote_xinput.vigem_bridge import DryRunPad


def wait_until(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def make_manager(tmp_path, transport, pads):
    store = ConfigStore(tmp_path / "c.json").load()
    store.settings.scan_interval_s = 0.5
    store.settings.reconnect_grace_s = 1.0

    def pad_factory():
        pad = DryRunPad()
        pads.append(pad)
        return pad

    return store, DeviceManager(store, pad_factory, lambda: [SIMULATED_INFO], lambda info: transport)


def test_connects_initialises_nunchuk_and_streams(tmp_path):
    transport, pads, events = SimulatedTransport(), [], []
    store, mgr = make_manager(tmp_path, transport, pads)
    mgr.add_listener(lambda ev, info: events.append((ev, info)))
    mgr.start()
    try:
        assert wait_until(lambda: mgr.sessions() and mgr.sessions()[0].info().status == ConnectionStatus.CONNECTED)
        s = mgr.sessions()[0]
        assert wait_until(lambda: s.state.nunchuk is not None)
        info = s.info()
        assert info.slot == 1 and info.extension == ExtensionType.NUNCHUK
        assert info.battery_percent == 80
        assert transport.leds == 0x10  # player 1 LED
        assert transport.mode == 0x35 and transport.continuous
        assert wait_until(lambda: pads[0].submits > 5)
        assert events[0][0] == "added"

        # Rumble from the game reaches the Wiimote's rumble bit.
        pads[0].fake_rumble(255, 0)
        assert wait_until(lambda: transport.rumble)
        pads[0].fake_rumble(0, 0)
        assert wait_until(lambda: not transport.rumble)

        # Switching to a profile that needs IR reconfigures the reporting mode.
        mgr.apply_profile(store.profiles["IR Pointer (right stick)"])
        assert wait_until(lambda: transport.mode == 0x37)
        assert wait_until(lambda: s.state.pointer is not None)
    finally:
        mgr.stop()
    assert pads[0].closed


def test_disconnect_releases_inputs_and_reconnects(tmp_path):
    transport, pads = SimulatedTransport(), []
    _store, mgr = make_manager(tmp_path, transport, pads)
    mgr.start()
    try:
        assert wait_until(lambda: mgr.sessions() and pads and pads[0].submits > 3)
        s = mgr.sessions()[0]
        transport.connected = False
        assert wait_until(lambda: s.info().status == ConnectionStatus.RECONNECTING)
        assert pads[0].last.buttons == 0 and pads[0].last.lx == 0  # nothing stuck
        transport.connected = True
        assert wait_until(lambda: s.info().status == ConnectionStatus.CONNECTED)
        assert len(pads) == 1  # same virtual controller kept across the drop
    finally:
        mgr.stop()


def test_powered_off_remote_is_not_shown(tmp_path):
    transport, pads = SimulatedTransport(), []
    transport.connected = False
    _store, mgr = make_manager(tmp_path, transport, pads)
    mgr.start()
    try:
        time.sleep(1.2)
        assert mgr.sessions() == [] and pads == []
    finally:
        mgr.stop()


def test_ir_is_reinitialised_after_reconnect(tmp_path):
    transport, pads = SimulatedTransport(), []
    store, mgr = make_manager(tmp_path, transport, pads)
    store.settings.active_profile = "IR Pointer (right stick)"
    mgr.start()
    try:
        assert wait_until(lambda: mgr.sessions() and transport.mode == 0x37)
        s = mgr.sessions()[0]
        transport.connected = False
        assert wait_until(lambda: s.info().status == ConnectionStatus.RECONNECTING)
        transport.writes.clear()
        transport.connected = True
        assert wait_until(lambda: s.info().status == ConnectionStatus.CONNECTED)
        assert any(w[0] == 0x13 and w[1] & 0x04 for w in transport.writes)  # camera re-enabled
        assert wait_until(lambda: transport.mode == 0x37)
    finally:
        mgr.stop()


def test_keyboard_mouse_profile_end_to_end(tmp_path):
    from wiimote_xinput.kbm_output import KbmOutputFactory, RecordingBackend

    transport, pads = SimulatedTransport(), []
    store, _ = make_manager(tmp_path, transport, pads)
    backend = RecordingBackend()
    mgr = DeviceManager(store, lambda: pads.append(DryRunPad()) or pads[-1], lambda: [SIMULATED_INFO],
                        lambda info: transport, kbm_factory=KbmOutputFactory(backend))
    store.settings.active_profile = "Keyboard & Mouse FPS (Nunchuk WASD)"
    mgr.start()
    try:
        assert wait_until(lambda: mgr.sessions() and transport.mode == 0x37)  # IR enabled for the mouse
        # The simulator circles the Nunchuk stick, so movement keys and mouse motion appear.
        assert wait_until(lambda: any(e[0] == "key" and e[1] in "WASD" for e in backend.events))
        assert wait_until(lambda: any(e[0] == "move" for e in backend.events))
        assert all(p.last.buttons == 0 for p in pads)  # Xbox output idle in this profile
        transport.connected = False
        s = mgr.sessions()[0]
        assert wait_until(lambda: s.info().status == ConnectionStatus.RECONNECTING)
        downs = sum(1 for e in backend.events if e[0] in ("key", "button") and e[2])
        ups = sum(1 for e in backend.events if e[0] in ("key", "button") and not e[2])
        assert downs == ups  # nothing left held down after the drop
    finally:
        mgr.stop()
