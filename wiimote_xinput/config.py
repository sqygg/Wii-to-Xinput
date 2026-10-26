"""Profiles and application settings, persisted as JSON.

Location: ``%APPDATA%\\WiimoteXInput\\config.json`` on Windows,
``~/.config/wiimote-xinput/config.json`` elsewhere (useful for development).
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .constants import (
    BUTTON_SOURCES,
    ORIENTATIONS,
    STICK_SOURCES,
    TRIGGER_SOURCES,
    XINPUT_TARGETS,
)

log = logging.getLogger(__name__)

APP_NAME = "WiimoteXInput"
CONFIG_VERSION = 1


def config_dir() -> Path:
    override = os.environ.get("WIIMOTE_XINPUT_CONFIG_DIR")
    if override:
        return Path(override)
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    return Path.home() / ".config" / "wiimote-xinput"


def _clampf(value: Any, lo: float, hi: float, default: float) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    return max(lo, min(hi, v))


def _bool(value: Any, default: bool) -> bool:
    return value if isinstance(value, bool) else default


@dataclass
class StickConfig:
    source: str = "none"
    deadzone: float = 0.12  # radial inner deadzone (0..0.9)
    saturation: float = 0.95  # magnitude treated as full deflection
    curve: float = 1.0  # response exponent (1 = linear, >1 = finer centre control)
    invert_x: bool = False
    invert_y: bool = False
    tilt_range_deg: float = 45.0  # only for *.tilt sources

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, default: "StickConfig") -> "StickConfig":
        d = d or {}
        source = d.get("source", default.source)
        return cls(
            source=source if source in STICK_SOURCES else default.source,
            deadzone=_clampf(d.get("deadzone"), 0.0, 0.9, default.deadzone),
            saturation=_clampf(d.get("saturation"), 0.3, 1.0, default.saturation),
            curve=_clampf(d.get("curve"), 0.3, 3.0, default.curve),
            invert_x=_bool(d.get("invert_x"), default.invert_x),
            invert_y=_bool(d.get("invert_y"), default.invert_y),
            tilt_range_deg=_clampf(d.get("tilt_range_deg"), 10.0, 90.0, default.tilt_range_deg),
        )


@dataclass
class TriggerConfig:
    source: str = "none"
    deadzone: float = 0.05

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None, default: "TriggerConfig") -> "TriggerConfig":
        d = d or {}
        source = d.get("source", default.source)
        return cls(
            source=source if source in TRIGGER_SOURCES else default.source,
            deadzone=_clampf(d.get("deadzone"), 0.0, 0.9, default.deadzone),
        )


DEFAULT_BUTTONS: dict[str, str] = {
    # Wiimote (vertical)
    "wiimote.UP": "DPAD_UP",
    "wiimote.DOWN": "DPAD_DOWN",
    "wiimote.LEFT": "DPAD_LEFT",
    "wiimote.RIGHT": "DPAD_RIGHT",
    "wiimote.A": "A",
    "wiimote.B": "B",
    "wiimote.ONE": "X",
    "wiimote.TWO": "Y",
    "wiimote.MINUS": "BACK",
    "wiimote.PLUS": "START",
    "wiimote.HOME": "GUIDE",
    # Nunchuk
    "nunchuk.C": "LB",
    "nunchuk.Z": "RB",
    # Classic Controller: positional layout (Nintendo b/a/y/x -> Xbox A/B/X/Y)
    "classic.UP": "DPAD_UP",
    "classic.DOWN": "DPAD_DOWN",
    "classic.LEFT": "DPAD_LEFT",
    "classic.RIGHT": "DPAD_RIGHT",
    "classic.B": "A",
    "classic.A": "B",
    "classic.Y": "X",
    "classic.X": "Y",
    "classic.ZL": "LB",
    "classic.ZR": "RB",
    "classic.L": "LT",
    "classic.R": "RT",
    "classic.MINUS": "BACK",
    "classic.PLUS": "START",
    "classic.HOME": "GUIDE",
}


@dataclass
class Profile:
    name: str = "Default"
    orientation: str = "vertical"
    buttons: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_BUTTONS))
    left_stick: StickConfig = field(default_factory=lambda: StickConfig(source="nunchuk.stick"))
    right_stick: StickConfig = field(default_factory=lambda: StickConfig(source="classic.right_stick"))
    left_trigger: TriggerConfig = field(default_factory=lambda: TriggerConfig(source="classic.lt"))
    right_trigger: TriggerConfig = field(default_factory=lambda: TriggerConfig(source="classic.rt"))
    # When a Classic Controller is plugged in, its left stick also drives the
    # left thumbstick (in addition to the configured source).
    classic_left_stick_fallback: bool = True
    rumble_enabled: bool = True
    rumble_mode: str = "pwm"  # "pwm" (intensity via duty cycle) or "threshold"
    rumble_threshold: int = 40  # 0..255 motor level below which rumble is off

    def needs_ir(self) -> bool:
        return "ir.pointer" in (self.left_stick.source, self.right_stick.source)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Profile":
        base = cls()
        name = str(d.get("name") or base.name).strip()[:64] or base.name
        orientation = d.get("orientation", base.orientation)
        buttons_in = d.get("buttons")
        if isinstance(buttons_in, dict):
            buttons = {
                src: tgt
                for src, tgt in buttons_in.items()
                if src in BUTTON_SOURCES and tgt in XINPUT_TARGETS
            }
        else:
            buttons = dict(base.buttons)
        mode = d.get("rumble_mode", base.rumble_mode)
        return cls(
            name=name,
            orientation=orientation if orientation in ORIENTATIONS else base.orientation,
            buttons=buttons,
            left_stick=StickConfig.from_dict(d.get("left_stick"), base.left_stick),
            right_stick=StickConfig.from_dict(d.get("right_stick"), base.right_stick),
            left_trigger=TriggerConfig.from_dict(d.get("left_trigger"), base.left_trigger),
            right_trigger=TriggerConfig.from_dict(d.get("right_trigger"), base.right_trigger),
            classic_left_stick_fallback=_bool(
                d.get("classic_left_stick_fallback"), base.classic_left_stick_fallback
            ),
            rumble_enabled=_bool(d.get("rumble_enabled"), base.rumble_enabled),
            rumble_mode=mode if mode in ("pwm", "threshold") else base.rumble_mode,
            rumble_threshold=int(_clampf(d.get("rumble_threshold"), 0, 255, base.rumble_threshold)),
        )

    def copy(self, name: str | None = None) -> "Profile":
        p = copy.deepcopy(self)
        if name is not None:
            p.name = name
        return p


def default_profiles() -> dict[str, Profile]:
    default = Profile()

    sideways = Profile(name="Sideways (NES style)", orientation="sideways")
    sideways.buttons.update(
        {
            "wiimote.TWO": "A",
            "wiimote.ONE": "B",
            "wiimote.A": "X",
            "wiimote.B": "Y",
        }
    )
    sideways.left_stick = StickConfig(source="none")
    sideways.right_stick = StickConfig(source="none")

    racing = Profile(name="Sideways Racing (tilt steering)", orientation="sideways")
    racing.buttons.update(
        {"wiimote.TWO": "RT", "wiimote.ONE": "LT", "wiimote.A": "X", "wiimote.B": "Y"}
    )
    racing.left_stick = StickConfig(source="wiimote.tilt", deadzone=0.05, tilt_range_deg=40.0)
    racing.right_stick = StickConfig(source="none")

    pointer = Profile(name="IR Pointer (right stick)")
    pointer.right_stick = StickConfig(source="ir.pointer", deadzone=0.04, saturation=0.85)

    return {p.name: p for p in (default, sideways, racing, pointer)}


@dataclass
class AppSettings:
    active_profile: str = "Default"
    minimize_to_tray: bool = True
    close_to_tray: bool = True
    start_minimized: bool = False
    notifications: bool = True
    auto_start: bool = False
    reconnect_grace_s: float = 15.0  # keep the virtual pad plugged in this long after a drop
    battery_poll_s: float = 60.0
    scan_interval_s: float = 2.0
    write_method: str = "auto"  # "auto", "hidapi" (WriteFile) or "set_output_report"

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> "AppSettings":
        d = d or {}
        base = cls()
        wm = d.get("write_method", base.write_method)
        return cls(
            active_profile=str(d.get("active_profile") or base.active_profile),
            minimize_to_tray=_bool(d.get("minimize_to_tray"), base.minimize_to_tray),
            close_to_tray=_bool(d.get("close_to_tray"), base.close_to_tray),
            start_minimized=_bool(d.get("start_minimized"), base.start_minimized),
            notifications=_bool(d.get("notifications"), base.notifications),
            auto_start=_bool(d.get("auto_start"), base.auto_start),
            reconnect_grace_s=_clampf(d.get("reconnect_grace_s"), 0.0, 300.0, base.reconnect_grace_s),
            battery_poll_s=_clampf(d.get("battery_poll_s"), 10.0, 600.0, base.battery_poll_s),
            scan_interval_s=_clampf(d.get("scan_interval_s"), 0.5, 30.0, base.scan_interval_s),
            write_method=wm if wm in ("auto", "hidapi", "set_output_report") else base.write_method,
        )


class ConfigStore:
    """Thread-safe owner of the settings + profile collection."""

    def __init__(self, path: Path | None = None):
        self.path = path or (config_dir() / "config.json")
        self._lock = threading.RLock()
        self.settings = AppSettings()
        self.profiles: dict[str, Profile] = default_profiles()

    # -- persistence -------------------------------------------------------

    def load(self) -> "ConfigStore":
        with self._lock:
            if not self.path.exists():
                log.info("No config found at %s, using defaults", self.path)
                self.save()
                return self
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise ValueError("top-level JSON value is not an object")
            except (OSError, ValueError) as exc:
                backup = self.path.with_suffix(f".corrupt-{int(time.time())}.json")
                log.error("Config file unreadable (%s); backing up to %s", exc, backup)
                try:
                    self.path.replace(backup)
                except OSError:
                    pass
                self.save()
                return self

            self.settings = AppSettings.from_dict(raw.get("settings"))
            profiles: dict[str, Profile] = {}
            for item in raw.get("profiles", []):
                if isinstance(item, dict):
                    p = Profile.from_dict(item)
                    profiles[p.name] = p
            self.profiles = profiles or default_profiles()
            if self.settings.active_profile not in self.profiles:
                self.settings.active_profile = next(iter(self.profiles))
            return self

    def save(self) -> None:
        with self._lock:
            data = {
                "version": CONFIG_VERSION,
                "settings": asdict(self.settings),
                "profiles": [p.to_dict() for p in self.profiles.values()],
            }
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
                os.replace(tmp, self.path)  # atomic on NTFS and POSIX
            except OSError as exc:
                log.error("Failed to save config to %s: %s", self.path, exc)

    # -- profile helpers -----------------------------------------------------

    @property
    def active_profile(self) -> Profile:
        with self._lock:
            return self.profiles.get(self.settings.active_profile) or next(iter(self.profiles.values()))

    def set_active(self, name: str) -> Profile:
        with self._lock:
            if name not in self.profiles:
                raise KeyError(name)
            self.settings.active_profile = name
            self.save()
            return self.profiles[name]

    def put_profile(self, profile: Profile, old_name: str | None = None, save: bool = True) -> None:
        with self._lock:
            if old_name and old_name != profile.name:
                # Rebuild the dict so a renamed profile keeps its position.
                self.profiles = {
                    (profile.name if k == old_name else k): (profile if k == old_name else v)
                    for k, v in self.profiles.items()
                }
                if self.settings.active_profile == old_name:
                    self.settings.active_profile = profile.name
            self.profiles[profile.name] = profile
            if save:
                self.save()

    def delete_profile(self, name: str) -> None:
        with self._lock:
            if len(self.profiles) <= 1:
                raise ValueError("Cannot delete the last profile")
            self.profiles.pop(name, None)
            if self.settings.active_profile == name:
                self.settings.active_profile = next(iter(self.profiles))
            self.save()

    def unique_name(self, base: str) -> str:
        with self._lock:
            if base not in self.profiles:
                return base
            i = 2
            while f"{base} ({i})" in self.profiles:
                i += 1
            return f"{base} ({i})"

    def import_profile(self, path: Path) -> Profile:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Profile file must contain a JSON object")
        profile = Profile.from_dict(data)
        profile.name = self.unique_name(profile.name)
        self.put_profile(profile)
        return profile

    @staticmethod
    def export_profile(profile: Profile, path: Path) -> None:
        Path(path).write_text(json.dumps(profile.to_dict(), indent=2), encoding="utf-8")
