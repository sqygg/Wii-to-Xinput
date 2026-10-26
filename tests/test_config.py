import json

from wiimote_xinput.config import ConfigStore, Profile


def test_roundtrip(tmp_path):
    store = ConfigStore(tmp_path / "config.json").load()
    p = store.active_profile.copy("Mine")
    p.left_stick.deadzone = 0.3
    p.buttons["wiimote.A"] = "Y"
    store.put_profile(p)
    store.set_active("Mine")
    again = ConfigStore(tmp_path / "config.json").load()
    assert again.settings.active_profile == "Mine"
    assert again.active_profile.left_stick.deadzone == 0.3
    assert again.active_profile.buttons["wiimote.A"] == "Y"


def test_sanitizes_bad_values():
    p = Profile.from_dict({
        "name": "x", "orientation": "upside-down",
        "buttons": {"wiimote.A": "NOT_A_BUTTON", "bogus": "A", "wiimote.B": "RT"},
        "left_stick": {"source": "???", "deadzone": 5, "invert_x": "yes"},
        "rumble_threshold": -4,
    })
    assert p.orientation == "vertical"
    assert p.buttons == {"wiimote.B": "RT"}
    assert p.left_stick.source == "nunchuk.stick" and p.left_stick.deadzone == 0.9
    assert p.left_stick.invert_x is False
    assert p.rumble_threshold == 0


def test_corrupt_file_is_backed_up(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{not json")
    store = ConfigStore(path).load()
    assert store.active_profile.name == "Default"
    assert json.loads(path.read_text())["version"] == 1
    assert list(tmp_path.glob("config.corrupt-*.json"))


def test_rename_keeps_active_and_order(tmp_path):
    store = ConfigStore(tmp_path / "c.json").load()
    names = list(store.profiles)
    p = store.active_profile.copy("Renamed")
    store.put_profile(p, old_name=names[0])
    assert list(store.profiles)[0] == "Renamed"
    assert store.settings.active_profile == "Renamed"


def test_import_export(tmp_path):
    store = ConfigStore(tmp_path / "c.json").load()
    out = tmp_path / "p.json"
    ConfigStore.export_profile(store.active_profile, out)
    imported = store.import_profile(out)
    assert imported.name == "Default (2)"
