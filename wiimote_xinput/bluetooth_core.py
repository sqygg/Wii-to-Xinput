"""Device discovery, per-Wiimote session threads and reconnection logic.

Threading model
---------------
* ``DeviceManager`` runs one scanner thread that enumerates Wiimote HID
  devices every ``scan_interval_s`` seconds.
* Each Wiimote gets a ``WiimoteSession`` thread that owns its HID handle and
  does *all* reads. The hot path is event driven: a blocking read (10 ms
  timeout, only so the thread can notice a stop request) -> parse -> map ->
  ViGEm update. No sleeps or polling sit between a report arriving and the
  virtual controller updating, so added latency is well under 1 ms.
* Rumble requests arrive on a ViGEm callback thread and only set a float;
  the session thread turns that into Wiimote output reports.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable

from .calibration import DeviceCalibration, EffectiveCalibration, RawSnapshot, apply_overrides
from .config import AppSettings, ConfigStore, Profile
from .constants import (
    EEPROM_ACCEL_CALIBRATION,
    IR_MODE_BASIC,
    IR_SENSITIVITY_BLOCK_1,
    IR_SENSITIVITY_BLOCK_2,
    MAX_PLAYERS,
    MEM_SPACE_EEPROM,
    MEM_SPACE_REGISTER,
    MODE_CONTINUOUS_FLAG,
    MODE_DEFAULT,
    MODE_WITH_IR,
    PLAYER_LEDS,
    REG_EXT_CALIBRATION,
    REG_EXT_ID,
    REG_EXT_INIT_1,
    REG_EXT_INIT_2,
    REG_IR_CONTROL,
    REG_IR_MODE,
    REG_IR_SENSITIVITY_1,
    REG_IR_SENSITIVITY_2,
    ExtensionType,
    OutputReport,
    identify_extension,
)
from .hid_transport import DeviceDisconnected, HidTransport, Transport, WiimoteHidInfo, enumerate_wiimotes
from .kbm_output import KeyboardMouseOutput
from .mapper import KBM_IDLE, NEUTRAL, KbmState, Mapper, XInputState
from .motion import MotionDetector
from .parser import (
    EMPTY_STATE,
    AccelCalibration,
    Acknowledge,
    ClassicCalibration,
    DataReport,
    MemoryReadResponse,
    NunchukCalibration,
    StatusReport,
    WiimoteState,
    classic_raw,
    ir_pointer,
    nunchuk_raw,
    parse_classic,
    parse_input_report,
    parse_ir,
    parse_nunchuk,
)
from .vigem_bridge import VirtualPad

log = logging.getLogger(__name__)

READ_TIMEOUT_MS = 10
HANDSHAKE_TIMEOUT_S = 1.5
MEMORY_TIMEOUT_S = 1.0
SILENCE_PROBE_S = 2.0  # no reports for this long -> send a status request
SILENCE_DISCONNECT_S = 6.0  # still nothing -> treat as disconnected
RUMBLE_PWM_PERIOD_S = 0.08


class ProtocolTimeout(Exception):
    """The Wiimote did not answer a request in time (but may still be alive)."""


class WiimoteMemoryError(Exception):
    def __init__(self, address: int, code: int):
        super().__init__(f"memory access at 0x{address:06X} failed with error {code}")
        self.address = address
        self.code = code


class ConnectionStatus(str, Enum):
    CONNECTING = "Connecting"
    CONNECTED = "Connected"
    RECONNECTING = "Reconnecting"
    DISCONNECTED = "Disconnected"


@dataclass(frozen=True)
class SessionInfo:
    key: str
    slot: int
    name: str
    model: str
    serial: str
    status: ConnectionStatus
    battery_percent: int | None
    battery_low: bool
    extension: ExtensionType
    ir_enabled: bool
    report_rate_hz: float
    last_error: str | None = None


TransportFactory = Callable[[WiimoteHidInfo], Transport]
PadFactory = Callable[[], VirtualPad]
KbmFactory = Callable[[], KeyboardMouseOutput]
SessionListener = Callable[[str, SessionInfo], None]  # (event, info)


class WiimoteSession(threading.Thread):
    def __init__(
        self,
        hid_info: WiimoteHidInfo,
        profile: Profile,
        settings: AppSettings,
        transport_factory: TransportFactory,
        pad_factory: PadFactory,
        slot_allocator: "SlotAllocator",
        listener: SessionListener | None = None,
        kbm_factory: KbmFactory | None = None,
        user_calibration: DeviceCalibration | None = None,
    ):
        super().__init__(daemon=True, name=f"Wiimote-{hid_info.key[-8:]}")
        self.hid_info = hid_info
        self._settings = settings
        self._transport_factory = transport_factory
        self._pad_factory = pad_factory
        self._slots = slot_allocator
        self._listener = listener
        self._mapper = Mapper(profile)

        self._stop_evt = threading.Event()
        self._transport: Transport | None = None
        self._pad: VirtualPad | None = None
        self._kbm = kbm_factory() if kbm_factory is not None else None
        self._motion = MotionDetector()
        self.ever_connected = False

        # Device state (written by the session thread only)
        self.slot = 0
        self._status = ConnectionStatus.CONNECTING
        self._battery: int | None = None
        self._battery_low = False
        self._extension = ExtensionType.NONE
        self._ext_retry_at = 0.0
        self._ir_enabled = False
        # Factory calibration as read from the device; user overrides are
        # layered on top into self._cal (replaced atomically, never mutated).
        self._factory_accel = AccelCalibration()
        self._factory_nunchuk = NunchukCalibration()
        self._factory_classic = ClassicCalibration()
        self._user_cal = user_calibration
        self._cal_lock = threading.Lock()
        self._cal: EffectiveCalibration = apply_overrides(
            self._factory_accel, self._factory_nunchuk, self._factory_classic, user_calibration
        )
        # Raw sensor snapshots for the Calibration tab (only built while enabled)
        self.raw_enabled = False
        self.raw: RawSnapshot | None = None
        self._deferred_status: StatusReport | None = None
        self._last_error: str | None = None
        self._reconfigure = False

        # Rumble
        self._rumble_level = 0.0  # 0..1 requested by the game
        self._rumble_on = False  # current motor state on the Wiimote

        # Live snapshot for the UI (replaced atomically, never mutated)
        self.state: WiimoteState = EMPTY_STATE
        self.output: XInputState = NEUTRAL
        self.kbm: KbmState = KBM_IDLE
        self._report_count = 0
        self._rate_window_start = time.monotonic()
        self._report_rate = 0.0

    # ------------------------------------------------------------------ API

    def stop(self) -> None:
        self._stop_evt.set()

    def set_profile(self, profile: Profile) -> None:
        old_ir = self._mapper.profile.needs_ir()
        self._mapper.set_profile(profile)
        if profile.needs_ir() != old_ir:
            self._reconfigure = True

    def set_user_calibration(self, cal: DeviceCalibration | None) -> None:
        """Apply new user calibration immediately (thread-safe)."""
        self._user_cal = cal
        self._rebuild_calibration()

    def _rebuild_calibration(self) -> None:
        with self._cal_lock:
            self._cal = apply_overrides(self._factory_accel, self._factory_nunchuk, self._factory_classic, self._user_cal)

    @property
    def factory_calibration(self) -> tuple[AccelCalibration, NunchukCalibration, ClassicCalibration]:
        return self._factory_accel, self._factory_nunchuk, self._factory_classic

    @property
    def calibration(self) -> EffectiveCalibration:
        return self._cal

    def set_settings(self, settings: AppSettings) -> None:
        self._settings = settings

    def info(self) -> SessionInfo:
        return SessionInfo(
            key=self.hid_info.key,
            slot=self.slot,
            name=self.hid_info.product or "Wiimote",
            model=self.hid_info.model,
            serial=self.hid_info.serial,
            status=self._status,
            battery_percent=self._battery,
            battery_low=self._battery_low,
            extension=self._extension,
            ir_enabled=self._ir_enabled,
            report_rate_hz=self._report_rate,
            last_error=self._last_error,
        )

    # ------------------------------------------------------------ lifecycle

    def run(self) -> None:
        backoff = 0.5
        dropped_at: float | None = None
        try:
            while not self._stop_evt.is_set():
                try:
                    self._connect()
                    dropped_at = None
                    backoff = 0.5
                    self._last_error = None
                    self._set_status(ConnectionStatus.CONNECTED)
                    self._pump()
                except DeviceDisconnected as exc:
                    self._last_error = str(exc)
                    if self.ever_connected:
                        log.warning("P%d disconnected: %s", self.slot, exc)
                    else:
                        log.debug("%s not reachable: %s", self.hid_info.key, exc)
                except ProtocolTimeout as exc:
                    self._last_error = str(exc)
                    log.debug("%s handshake timed out: %s", self.hid_info.key, exc)
                except Exception as exc:  # noqa: BLE001 - keep the thread alive
                    self._last_error = repr(exc)
                    log.exception("Unexpected error in Wiimote session")
                finally:
                    self._release_inputs()
                    self._close_transport()

                if self._stop_evt.is_set() or not self.ever_connected:
                    # A remote that never answered is most likely paired but
                    # switched off; the manager re-probes it on the next scan.
                    break
                now = time.monotonic()
                dropped_at = dropped_at or now
                if now - dropped_at > self._settings.reconnect_grace_s:
                    log.info("P%d did not come back within %.0fs", self.slot, self._settings.reconnect_grace_s)
                    break
                self._set_status(ConnectionStatus.RECONNECTING)
                self._stop_evt.wait(backoff)
                backoff = min(backoff * 2, 4.0)
        finally:
            self._shutdown()

    def _shutdown(self) -> None:
        self._release_inputs()
        if self._pad is not None:
            try:
                self._pad.close()
            except Exception:  # noqa: BLE001
                log.debug("Error closing virtual pad", exc_info=True)
            self._pad = None
        was_connected = self.ever_connected
        self._status = ConnectionStatus.DISCONNECTED
        if self.slot:
            self._slots.release(self.slot)
        if was_connected:
            self._emit("removed")

    def _emit(self, event: str) -> None:
        if event == "updated" and not self.ever_connected:
            return  # the UI only learns about a remote once its handshake completed
        if self._listener is not None:
            try:
                self._listener(event, self.info())
            except Exception:  # noqa: BLE001
                log.exception("Session listener failed")

    def _set_status(self, status: ConnectionStatus) -> None:
        if status != self._status:
            self._status = status
            self._emit("updated")

    def _release_inputs(self) -> None:
        """Never leave buttons stuck down in a game after a drop."""
        self.output = NEUTRAL
        self.state = EMPTY_STATE
        self.kbm = KBM_IDLE
        self._motion.reset()
        self._rumble_on = False
        if self._kbm is not None:
            try:
                self._kbm.release_all()
            except Exception:  # noqa: BLE001
                log.debug("Failed to release keyboard/mouse", exc_info=True)
        if self._pad is not None:
            try:
                self._pad.neutral()
            except Exception:  # noqa: BLE001
                log.debug("Failed to neutralise pad", exc_info=True)

    def _close_transport(self) -> None:
        if self._transport is not None:
            try:
                self._transport.close()
            finally:
                self._transport = None

    # ------------------------------------------------------------ handshake

    def _connect(self) -> None:
        # A reconnected remote has been power-cycled: camera and extension
        # state must be set up from scratch.
        self._ir_enabled = False
        self._transport = self._transport_factory(self.hid_info)
        self._transport.open()

        status = self._request_status(HANDSHAKE_TIMEOUT_S)
        first_time = not self.ever_connected
        if not self.slot:
            self.slot = self._slots.allocate()
        self._send(OutputReport.LEDS, [PLAYER_LEDS.get(self.slot, 0x10)])
        self._apply_status(status)

        try:
            cal = AccelCalibration.from_eeprom(
                self._read_memory(EEPROM_ACCEL_CALIBRATION, 10, MEM_SPACE_EEPROM)
            )
            self._factory_accel = cal or AccelCalibration()
            self._rebuild_calibration()
            if cal is None:
                log.warning("Accelerometer calibration invalid, using defaults")
        except (ProtocolTimeout, WiimoteMemoryError) as exc:
            log.warning("Could not read accelerometer calibration: %s", exc)

        self._extension = ExtensionType.NONE
        if status.extension_connected:
            self._init_extension()
        self._configure_ir(self._mapper.profile.needs_ir())
        self._set_reporting_mode()

        if self._pad is None:
            self._pad = self._pad_factory()
            self._pad.set_rumble_callback(self._on_rumble)
        self.ever_connected = True
        self._status = ConnectionStatus.CONNECTED
        if first_time:
            log.info("P%d connected: %s %s", self.slot, self.hid_info.model, self.hid_info.serial)
            self._emit("added")
        else:
            log.info("P%d reconnected", self.slot)

    # ------------------------------------------------------------ main loop

    def _pump(self) -> None:
        transport = self._transport
        assert transport is not None
        now = time.monotonic()
        last_report = last_probe = last_battery = now

        while not self._stop_evt.is_set():
            if self._reconfigure:
                self._reconfigure = False
                self._configure_ir(self._mapper.profile.needs_ir())
                self._set_reporting_mode()
            if self._deferred_status is not None:
                status, self._deferred_status = self._deferred_status, None
                self._handle_status(status)

            self._update_rumble()
            data = transport.read(READ_TIMEOUT_MS)
            now = time.monotonic()

            if data:
                last_report = now
                self._handle_report(data)
            else:
                silent = now - last_report
                if silent > SILENCE_DISCONNECT_S:
                    raise DeviceDisconnected(f"no data for {silent:.1f}s")
                if silent > SILENCE_PROBE_S and now - last_probe > 1.0:
                    last_probe = now
                    self._send(OutputReport.STATUS_REQUEST, [0x00])  # raises if gone

            if now - last_battery > self._settings.battery_poll_s:
                last_battery = now
                self._send(OutputReport.STATUS_REQUEST, [0x00])

            if self._extension == ExtensionType.PARTIAL and now >= self._ext_retry_at:
                self._init_extension()
                self._set_reporting_mode()

            if now - self._rate_window_start >= 1.0:
                self._report_rate = self._report_count / (now - self._rate_window_start)
                self._report_count = 0
                self._rate_window_start = now

    def _handle_report(self, data: bytes) -> None:
        parsed = parse_input_report(data)
        if isinstance(parsed, DataReport):
            self._process_data(parsed)
        elif isinstance(parsed, StatusReport):
            self._handle_status(parsed)
        elif isinstance(parsed, Acknowledge) and parsed.error:
            log.debug("Wiimote NAK for report 0x%02X (error %d)", parsed.report, parsed.error)

    def _handle_status(self, status: StatusReport) -> None:
        self._apply_status(status)
        if status.extension_connected and self._extension == ExtensionType.NONE:
            self._init_extension()
        elif not status.extension_connected and self._extension != ExtensionType.NONE:
            log.info("P%d extension removed", self.slot)
            self._extension = ExtensionType.NONE
            self._emit("updated")
        # A status report always switches the Wiimote back to report 0x30,
        # so the reporting mode must be re-sent every time.
        self._set_reporting_mode()

    def _apply_status(self, status: StatusReport) -> None:
        changed = status.battery_percent != self._battery or status.battery_low != self._battery_low
        self._battery = status.battery_percent
        self._battery_low = status.battery_low
        if changed:
            if status.battery_low:
                log.warning("P%d battery low (%d%%)", self.slot, status.battery_percent)
            self._emit("updated")

    def _process_data(self, rep: DataReport) -> None:
        cal = self._cal
        buttons = rep.buttons if rep.buttons is not None else self.state.buttons
        accel = cal.accel.normalize(rep.accel_raw) if rep.accel_raw else self.state.accel
        nunchuk = classic = None
        if rep.ext_raw is not None:
            if self._extension == ExtensionType.NUNCHUK:
                nunchuk = parse_nunchuk(rep.ext_raw, cal.nunchuk)
            elif self._extension.is_classic:
                classic = parse_classic(rep.ext_raw, cal.classic)
        dots: tuple = ()
        pointer_raw = None
        if rep.ir_raw is not None and self._ir_enabled:
            dots = tuple(parse_ir(rep.ir_raw))
            pointer_raw = ir_pointer(dots)
        pointer = cal.ir.apply(pointer_raw)

        profile = self._mapper.profile
        motion = self._motion.update(
            accel, nunchuk.accel if nunchuk else None, profile.orientation == "sideways", profile.motion
        )
        state = WiimoteState(buttons, accel, nunchuk, classic, dots, pointer, motion)
        out, kbm = self._mapper.map_all(state)
        if self._pad is not None and out != self.output:
            self._pad.submit(out)
        if self._kbm is not None:
            self._kbm.apply(kbm, self._mapper.profile.mouse)
        self.kbm = kbm
        self.state = state
        self.output = out
        if self.raw_enabled:
            ext = rep.ext_raw
            self.raw = RawSnapshot(
                accel=rep.accel_raw or (self.raw.accel if self.raw else None),
                nunchuk=nunchuk_raw(ext) if ext is not None and self._extension == ExtensionType.NUNCHUK else None,
                classic=classic_raw(ext) if ext is not None and self._extension.is_classic else None,
                ir_dots=dots,
                pointer_raw=pointer_raw,
                state=state,
            )
        self._report_count += 1

    # ------------------------------------------------------------ protocol

    def _send(self, report_id: int, payload: Iterable[int]) -> None:
        if self._transport is None:
            raise DeviceDisconnected("transport is closed")
        body = bytearray(payload)
        if not body:
            body.append(0)
        if self._rumble_on:
            body[0] |= 0x01  # rumble bit lives in every output report
        else:
            body[0] &= 0xFE
        self._transport.write(bytes([report_id]) + bytes(body))

    def _wait_for(self, predicate: Callable[[object], bool], timeout_s: float):
        """Read reports until one matches. Data reports keep flowing to the
        virtual pad meanwhile; status reports are deferred."""
        assert self._transport is not None
        deadline = time.monotonic() + timeout_s
        while not self._stop_evt.is_set():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            data = self._transport.read(max(1, min(READ_TIMEOUT_MS * 5, int(remaining * 1000))))
            if not data:
                continue
            parsed = parse_input_report(data)
            if parsed is not None and predicate(parsed):
                return parsed
            if isinstance(parsed, StatusReport):
                self._deferred_status = parsed
            elif isinstance(parsed, DataReport):
                self._process_data(parsed)
        return None

    def _request_status(self, timeout_s: float) -> StatusReport:
        self._send(OutputReport.STATUS_REQUEST, [0x00])
        status = self._wait_for(lambda p: isinstance(p, StatusReport), timeout_s)
        if status is None:
            if self._deferred_status is not None:
                status, self._deferred_status = self._deferred_status, None
                return status
            raise ProtocolTimeout("no status report")
        return status

    def _read_memory(self, address: int, size: int, space: int = MEM_SPACE_REGISTER) -> bytes:
        self._send(
            OutputReport.READ_MEMORY,
            [space, (address >> 16) & 0xFF, (address >> 8) & 0xFF, address & 0xFF, (size >> 8) & 0xFF, size & 0xFF],
        )
        buf = bytearray()
        while len(buf) < size:
            expected = (address + len(buf)) & 0xFFFF
            rep = self._wait_for(
                lambda p: isinstance(p, MemoryReadResponse) and p.address == expected, MEMORY_TIMEOUT_S
            )
            if rep is None:
                raise ProtocolTimeout(f"memory read at 0x{address:06X} timed out")
            if rep.error:
                raise WiimoteMemoryError(address, rep.error)
            buf += rep.data
        return bytes(buf[:size])

    def _write_memory(self, address: int, data: bytes, space: int = MEM_SPACE_REGISTER) -> None:
        if not 1 <= len(data) <= 16:
            raise ValueError("write_memory supports 1..16 bytes")
        payload = [space, (address >> 16) & 0xFF, (address >> 8) & 0xFF, address & 0xFF, len(data)]
        payload += list(data) + [0] * (16 - len(data))
        self._send(OutputReport.WRITE_MEMORY, payload)
        ack = self._wait_for(
            lambda p: isinstance(p, Acknowledge) and p.report == OutputReport.WRITE_MEMORY, MEMORY_TIMEOUT_S
        )
        if ack is None:
            raise ProtocolTimeout(f"no ack for write at 0x{address:06X}")
        if ack.error:
            raise WiimoteMemoryError(address, ack.error)

    def _set_reporting_mode(self) -> None:
        mode = MODE_WITH_IR if self._ir_enabled else MODE_DEFAULT
        self._send(OutputReport.DATA_REPORTING_MODE, [MODE_CONTINUOUS_FLAG, int(mode)])

    def _init_extension(self) -> None:
        """Unencrypted init sequence (works for genuine and third-party
        extensions), then identify and read calibration."""
        ext = ExtensionType.UNKNOWN
        try:
            self._write_memory(REG_EXT_INIT_1, b"\x55")
            self._write_memory(REG_EXT_INIT_2, b"\x00")
            ident = self._read_memory(REG_EXT_ID, 6)
            ext = identify_extension(ident)
            log.info("P%d extension: %s (id %s)", self.slot, ext.label, ident.hex(" "))
            if ext == ExtensionType.NUNCHUK:
                self._factory_nunchuk = NunchukCalibration.from_bytes(self._read_memory(REG_EXT_CALIBRATION, 16))
                self._rebuild_calibration()
            elif ext.is_classic:
                self._factory_classic = ClassicCalibration.from_bytes(self._read_memory(REG_EXT_CALIBRATION, 16))
                self._rebuild_calibration()
        except (ProtocolTimeout, WiimoteMemoryError) as exc:
            log.warning("P%d extension init failed: %s", self.slot, exc)
            ext = ExtensionType.PARTIAL
        if ext == ExtensionType.PARTIAL:
            self._ext_retry_at = time.monotonic() + 1.0
        self._extension = ext
        self._emit("updated")

    def _configure_ir(self, enable: bool) -> None:
        if enable == self._ir_enabled:
            return
        try:
            if enable:
                self._send(OutputReport.IR_CAMERA_ENABLE, [0x04])
                self._send(OutputReport.IR_CAMERA_ENABLE_2, [0x04])
                self._write_memory(REG_IR_CONTROL, b"\x08")
                self._write_memory(REG_IR_SENSITIVITY_1, IR_SENSITIVITY_BLOCK_1)
                self._write_memory(REG_IR_SENSITIVITY_2, IR_SENSITIVITY_BLOCK_2)
                self._write_memory(REG_IR_MODE, bytes([IR_MODE_BASIC]))
                self._write_memory(REG_IR_CONTROL, b"\x08")
            else:
                self._send(OutputReport.IR_CAMERA_ENABLE, [0x00])
                self._send(OutputReport.IR_CAMERA_ENABLE_2, [0x00])
            self._ir_enabled = enable
            log.info("P%d IR camera %s", self.slot, "enabled" if enable else "disabled")
        except (ProtocolTimeout, WiimoteMemoryError) as exc:
            log.warning("P%d IR camera setup failed: %s", self.slot, exc)
            self._ir_enabled = False
        self._emit("updated")

    # ------------------------------------------------------------ rumble

    def _on_rumble(self, large: int, small: int) -> None:
        self._rumble_level = max(large, small) / 255.0

    def _update_rumble(self) -> None:
        profile = self._mapper.profile
        level = self._rumble_level if profile.rumble_enabled else 0.0
        threshold = profile.rumble_threshold / 255.0
        if level <= threshold or level <= 0.0:
            want = False
        elif profile.rumble_mode == "threshold" or level >= 0.9:
            want = True
        else:  # PWM: the Wiimote motor is on/off, so modulate the duty cycle
            phase = (time.monotonic() % RUMBLE_PWM_PERIOD_S) / RUMBLE_PWM_PERIOD_S
            want = phase < level
        if want != self._rumble_on:
            self._rumble_on = want
            self._send(OutputReport.RUMBLE, [0x00])


class SlotAllocator:
    """Hands out player numbers 1..4 (shown on the Wiimote LEDs)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._used: set[int] = set()

    def allocate(self) -> int:
        with self._lock:
            for slot in range(1, MAX_PLAYERS + 1):
                if slot not in self._used:
                    self._used.add(slot)
                    return slot
            # More than four remotes: share LED pattern 4, still fully usable.
            slot = max(self._used, default=0) + 1
            self._used.add(slot)
            return slot

    def release(self, slot: int) -> None:
        with self._lock:
            self._used.discard(slot)


class DeviceManager:
    """Discovers Wiimotes and owns their sessions."""

    def __init__(
        self,
        store: ConfigStore,
        pad_factory: PadFactory,
        enumerate_fn: Callable[[], list[WiimoteHidInfo]] = enumerate_wiimotes,
        transport_factory: TransportFactory | None = None,
        kbm_factory: KbmFactory | None = None,
    ):
        self.store = store
        self._pad_factory = pad_factory
        self._kbm_factory = kbm_factory
        self._enumerate = enumerate_fn
        self._transport_factory = transport_factory or (
            lambda info: HidTransport(info.path, self.store.settings.write_method)
        )
        self._sessions: dict[str, WiimoteSession] = {}
        self._lock = threading.Lock()
        self._slots = SlotAllocator()
        self._listeners: list[SessionListener] = []
        self._stop_evt = threading.Event()
        self._rescan_evt = threading.Event()
        self._thread: threading.Thread | None = None

    # -- listeners ---------------------------------------------------------

    def add_listener(self, listener: SessionListener) -> None:
        self._listeners.append(listener)

    def _dispatch(self, event: str, info: SessionInfo) -> None:
        for listener in list(self._listeners):
            try:
                listener(event, info)
            except Exception:  # noqa: BLE001
                log.exception("Manager listener failed")

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._scan_loop, name="WiimoteScanner", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        self._stop_evt.set()
        self._rescan_evt.set()
        if self._thread is not None:
            self._thread.join(timeout)
            self._thread = None
        with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for s in sessions:
            s.stop()
        for s in sessions:
            s.join(timeout)

    def rescan(self) -> None:
        self._rescan_evt.set()

    def _scan_loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self._scan_once()
            except Exception:  # noqa: BLE001 - e.g. hidapi failing transiently
                log.exception("Device scan failed")
            self._rescan_evt.wait(self.store.settings.scan_interval_s)
            self._rescan_evt.clear()

    def _scan_once(self) -> None:
        devices = self._enumerate()
        with self._lock:
            for key in [k for k, s in self._sessions.items() if not s.is_alive()]:
                del self._sessions[key]
            for info in devices:
                if info.key in self._sessions:
                    continue
                session = WiimoteSession(
                    info,
                    self.store.active_profile,
                    self.store.settings,
                    self._transport_factory,
                    self._pad_factory,
                    self._slots,
                    self._dispatch,
                    self._kbm_factory,
                    self.store.get_calibration(info.key),
                )
                self._sessions[info.key] = session
                session.start()

    # -- control -----------------------------------------------------------------

    def apply_profile(self, profile: Profile) -> None:
        with self._lock:
            for s in self._sessions.values():
                s.set_profile(profile)

    def set_calibration(self, key: str, cal: DeviceCalibration | None) -> None:
        """Persist a Wiimote's user calibration and apply it live."""
        self.store.set_calibration(key, cal)
        with self._lock:
            session = self._sessions.get(key)
        if session is not None:
            session.set_user_calibration(cal)

    def apply_settings(self, settings: AppSettings) -> None:
        with self._lock:
            for s in self._sessions.values():
                s.set_settings(settings)

    def sessions(self) -> list[WiimoteSession]:
        """Sessions that have completed a handshake at least once."""
        with self._lock:
            live = [s for s in self._sessions.values() if s.ever_connected and s.is_alive()]
        return sorted(live, key=lambda s: s.slot)
