# Wiimote → XInput

Use a Nintendo Wii Remote (Wiimote), with or without a Nunchuk or Classic Controller, as a
**virtual Xbox 360 controller** on Windows 10/11. Steam, Xbox/Game Pass games and anything
else that speaks XInput see a normal wired Xbox 360 pad. No per-game setup is needed.

* Native Bluetooth HID: no Dolphin, no GlovePIE. Works with `RVL-CNT-01` and `RVL-CNT-01-TR` (Wii Remote Plus), and with a DolphinBar in mode 4.
* Nunchuk and Classic Controller / Classic Controller Pro are detected automatically, including hot-plugging, and use their factory calibration.
* Accelerometer tilt steering, an IR pointer mapped to a stick, and D-Pad-as-stick.
* **Calibration tab**: guided calibration for the accelerometers, the Nunchuk stick, the Classic Controller sticks and triggers, and the IR pointer, plus a deadzone helper and a live sensor monitor. Calibration is stored per Wiimote, applies instantly, and can be reset to factory values.
* **Bindable motion gestures**: shake, and tilt left/right/up/down, on both the Wiimote and the Nunchuk. Each gesture works like an extra button and can press any Xbox button, key or mouse button.
* **Keyboard & mouse output**, alongside the Xbox pad or instead of it. Any button can press a key or mouse button, or scroll the wheel. The IR pointer, a stick or tilt can move the mouse cursor, and a stick can press WASD or the arrow keys. This works in games that don't support controllers, and on the desktop.
* Game rumble is forwarded to the Wiimote motor, with variable intensity done by pulse-width modulation.
* Up to 4 remotes. Each one gets its own virtual controller and player LED.
* Automatic reconnection. Inputs are released immediately when a remote drops, so nothing stays stuck down.
* Desktop UI with a live view of the Xbox output, battery level, remapping and per-stick tuning. Runs from the system tray.
* JSON profiles that you can switch from the tray, import and export.
* A built-in pairing helper that gets around the Windows 11 "enter PIN" problem.

---

## 1. Architecture

### Why Python (and not C#)

Both stacks call the same native pieces: `hid.dll` (via hidapi) and `ViGEmClient` (via vgamepad,
or Nefarius.ViGEm.Client in C#). Python was chosen because:

* **Latency comes from Bluetooth, not the language.** A Wiimote sends a report about every 10 ms
  (~100 Hz). Per report, this app parses about 22 bytes, applies the mapping and makes one
  ViGEm IOCTL. That costs **well under 0.5 ms** in CPython. The hot path is event-driven: a blocking
  HID read wakes the thread as soon as a report arrives, and there is no polling loop or sleep
  between the radio and the virtual pad. Added latency is far below the 5 ms target.
* hidapi, vgamepad and PySide6 are mature, pip-installable and ship the native DLLs. vgamepad even
  bundles the official ViGEmBus installer, which the app can launch for you.
* The protocol code is pure Python and hardware-free, so all of it is unit tested on any OS, and
  a protocol-level simulator lets you run the full app without a Wiimote.

### Data flow

```
 Wiimote ──BT HID──► Windows HID stack ──► hid_transport.py ──► bluetooth_core.WiimoteSession (1 thread/remote)
                                            (hidapi ReadFile,       │  handshake, calibration, extension init,
                                             WriteFile or            │  IR setup, reconnect, rumble PWM
                                             HidD_SetOutputReport)   ▼
                                                               parser.py ──► WiimoteState (calibrated)
                                                                             │
                                                               mapper.py ──► XInputState (profile applied)
                                                                             │
                                                            vigem_bridge.py ──► ViGEmBus ──► "Xbox 360 Controller"
                                                                             ▲                 │
                                                       rumble ◄──────────────┴─────────────────┘ (game force feedback)
 ui.py / widgets.py: reads snapshots at 30 fps (never blocks the input path)
```

### Project layout

```
wiimote_xinput/
├── __main__.py        Entry point and CLI (--headless, --simulate, --dry-run, --pair, --list, …)
├── constants.py       Report IDs, memory map, button masks, extension IDs, XUSB constants
├── hid_transport.py   hidapi device I/O + HidD_SetOutputReport fallback, Wiimote enumeration
├── bluetooth_core.py  WiimoteSession (protocol state machine) + DeviceManager (discovery, slots)
├── bt_pairing.py      Win32 Bluetooth API pairing helper (binary PIN, HID service enable)
├── parser.py          Pure report parsing: buttons, accel, Nunchuk, Classic, IR, status, calibration
├── calibration.py     Per-Wiimote user calibration: data model, factory overrides, guided procedures
├── calibration_ui.py  Calibration tab: wizards, live stick / IR camera / accelerometer views, sensor monitor
├── motion.py          Shake / tilt gesture detection (hysteresis, hold time, swing rejection)
├── mapper.py          Profile → XInput + keyboard/mouse translation, deadzones, curves, tilt, sideways rotation
├── kbm_output.py      Keyboard & mouse injection via SendInput (scan codes, sub-pixel motion, shared key counts)
├── config.py          Profiles and settings dataclasses, validated JSON persistence
├── vigem_bridge.py    Virtual X360 pad (vgamepad), rumble callback, driver check, installer launcher
├── simulator.py       Byte-accurate Wiimote emulator (demo mode and tests)
├── platform_utils.py  Auto-start (HKCU\…\Run), Settings shortcuts
├── ui.py              Main window, mapping/settings editors, pairing dialog, system tray
└── widgets.py         Controller cards, live XInput visualiser, sliders, generated icons
tests/                 pytest suite: parser, mapper, config, end-to-end session against the simulator
scripts/build_exe.bat  PyInstaller one-file build
run.bat                First-run setup and launch
```

### Protocol details implemented

| Step | Wiimote reports used |
|---|---|
| Handshake / battery | `0x15` status request → `0x20` status (battery, extension flag, LEDs) |
| Player LEDs | `0x11` |
| Accelerometer calibration | `0x17` read EEPROM `0x0016` (10 bytes, checksum verified) → `0x21` |
| Extension init (unencrypted) | `0x16` write `0x55`→`0xA400F0`, `0x00`→`0xA400FB`; read ID at `0xA400FA`; read calibration at `0xA40020` |
| IR camera (only when a profile uses it) | `0x13`/`0x1A` enable, sensitivity blocks at `0xB00000`/`0xB0001A`, basic mode at `0xB00033` |
| Streaming | `0x12` continuous mode `0x35` (buttons + accel + 16 ext bytes), or `0x37` (+ 10 IR bytes) |
| Rumble | bit 0 of every output report, toggled through `0x10` |

The reporting mode is re-sent after **every** status report, because a status report silently
switches the Wiimote back to buttons-only mode. Missing this is a classic Wiimote driver bug.

---

## 2. Setup & installation

### Requirements

* Windows 10 or 11 (x64) with a Bluetooth adapter (built-in or USB). A DolphinBar also works.
* Python **3.10+** from [python.org](https://www.python.org/downloads/windows/). Tick *"Add python.exe to PATH"*.
* The **ViGEmBus** driver.

### Step 1: Get the code and install the dependencies

```powershell
git clone <this repository> wiimote-xinput
cd wiimote-xinput
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Or just double-click **`run.bat`** in Explorer. From PowerShell, type `.\run.bat` (PowerShell
only runs scripts in the current folder when they start with `.\`). It creates the virtual
environment on first launch, which takes a minute or two, then opens the app.

### Step 2: Install the ViGEmBus driver

Use any one of these:

* **From the app:** start it. If the driver is missing, a red banner shows an **Install ViGEmBus**
  button. It runs the official MSI that ships inside the `vgamepad` package.
* `pip install vgamepad` usually offers to run the installer automatically.
* Manually: download `ViGEmBus_*_x64_x86_arm64.exe` from
  <https://github.com/nefarius/ViGEmBus/releases/latest>.

Reboot if Windows asks you to, then click **Re-check** (or run `python -m wiimote_xinput --check-driver`).

> ViGEmBus has been retired by its author but remains the standard virtual-gamepad driver. The
> final 1.22 release works on current Windows 11 builds.

### Step 3: Pair the Wiimote

**Option A: the built-in helper (recommended).** It gets around the PIN prompt and pairs permanently.

1. In the app, click **Pair Wiimote…**. Or run `python -m wiimote_xinput --pair` from a terminal.
2. Remove the battery cover and press the small red **SYNC** button. The 4 LEDs start blinking.
3. Click **Start**. Within a few seconds you will see `Paired Nintendo RVL-CNT-01… (permanent)`.

After pairing with SYNC, pressing **any button** on the Wiimote reconnects it in the future.

**Option B: Windows Settings.**

1. *Settings → Bluetooth & devices → Add device → Bluetooth*.
2. Press **SYNC** (or hold **1 + 2**).
3. Pick **Nintendo RVL-CNT-01** (or `-TR`).
4. If a **PIN** is requested, leave it **empty** and click **Connect/Next**. Do not type anything.
   If Windows refuses an empty PIN, use Option A.

> If *"Show all devices"* lists the Wiimote as an "Input device" that keeps connecting and
> disconnecting, remove it and pair again with Option A.

### Step 4: Run

```powershell
python -m wiimote_xinput            # GUI with tray icon
python -m wiimote_xinput --minimized
python -m wiimote_xinput --headless # console only
```

Within about 2 seconds of connecting, the Wiimote's player LED lights up, and a card appears on
the dashboard with battery, extension and a live view of the Xbox output. Windows now lists an
*"Xbox 360 Controller for Windows"*. Check it in Steam (*Settings → Controller*) or with `joy.cpl`.

Enable *Settings → Start with Windows* to have it ready in the tray after every login.

### Optional: standalone .exe

```powershell
pip install -r requirements-dev.txt
scripts\build_exe.bat      # → dist\WiimoteXInput.exe
```

---

## 3. Default mapping

| Wiimote / extension | Xbox 360 |
|---|---|
| D-Pad | D-Pad |
| A / B | A / B |
| 1 / 2 | X / Y |
| − / + / Home | Back / Start / Guide |
| Nunchuk stick | Left stick |
| Nunchuk C / Z | LB / RB (or LT / RT, set in the Mapping tab) |
| Classic b / a / y / x | A / B / X / Y (by position: Nintendo's bottom button becomes Xbox's bottom button) |
| Classic L / R (analog) | LT / RT |
| Classic ZL / ZR | LB / RB |
| Classic sticks | Left / right stick |

Built-in profiles: **Default**, **Sideways (NES style)** (D-Pad rotated, 2 = A, 1 = B),
**Sideways Racing** (tilt steering, 2 = RT accelerator, 1 = LT brake), **IR Pointer**
(sensor bar aims with the right stick), and the two keyboard & mouse profiles below.

### Calibration

Factory calibration is read from every Wiimote and extension automatically, so calibrating is
optional. Do it when a stick drifts or can't reach the edge, tilt or shake feels off, or the IR
pointer doesn't line up with your screen. Open the **Calibration** tab and pick the Wiimote at the
top (each remote stores its own calibration). Then choose a procedure:

| Procedure | What you do | What it fixes |
|---|---|---|
| **Wiimote accelerometer** | Place the remote flat, standing on its end, then on its side (3 × 1.5 s, hands off) | Tilt sticks, tilt gestures, shake sensitivity |
| **Nunchuk accelerometer** | Same three positions with the Nunchuk | Nunchuk tilt and shake |
| **Nunchuk stick** | Let go (centre), then rotate the stick around its edge and click Done | Drift, off-centre rest position, sticks that never reach 100% |
| **Classic Controller** | Let go of everything, then rotate both sticks and fully press L/R | Both sticks, and the analog trigger travel (many controllers never reach full scale) |
| **IR pointer** | Aim at the screen centre, the top-left corner, then the bottom-right corner | Makes the pointer cover exactly your screen, wherever the sensor bar sits |
| **Deadzone helper** | Let go of all sticks for 3 s | Suggests the smallest drift-free deadzone and applies it to the active profile |

Each procedure checks its measurements. A remote that moved during a "keep still" step, was
in the wrong position, didn't move a stick far enough, or lost sight of the sensor bar gets a
clear message, and that step can be repeated. Results apply the moment you click **Save
calibration**. **Reset to factory** undoes one procedure, and **Reset all to factory** undoes
everything for that Wiimote.

The tab shows live visuals: the stick position against its calibrated range (dashed) and the
range measured so far (shaded), what the IR camera sees, and the acceleration per axis. The
**live sensor monitor** lists raw and calibrated values, tilt angles and shake strength
against your gesture thresholds, IR dots, active gestures, and which calibration is custom.
It is useful for tuning, and for placing the sensor bar.

After an IR calibration, set *Settings → Mouse pointer → Pointer range* to 100%, because the
calibrated pointer already spans the screen exactly.

### Motion gestures

The **Motion gestures** box at the bottom of the **Mapping** tab lists ten gestures. Bind each
one like a physical button, to an Xbox button, a key, a mouse button, or both:

| Gesture | Detected when |
|---|---|
| Wiimote / Nunchuk **shake** | the acceleration differs from normal gravity by more than the threshold (a waggle, a swing, a flick) |
| Wiimote / Nunchuk **tilt left / right** | the remote is rolled past the tilt angle. In sideways mode, the Wiimote is turned like a steering wheel |
| Wiimote / Nunchuk **tilt up / down** | the remote is pointed up or down past the tilt angle |

Examples: shake the Wiimote to reload (`motion.WIIMOTE_SHAKE` → `R`), shake the Nunchuk to dodge
(`motion.NUNCHUK_SHAKE` → `B`), lean the Wiimote to peek (`motion.WIIMOTE_TILT_LEFT` → `Q`,
`…_TILT_RIGHT` → `E`), or point the remote down to crouch.

*Settings → Motion gestures* adjusts how strong a shake must be, how long a shake stays pressed
(so a quick waggle registers as one press), and the tilt angle. Tilt releases a few degrees
below the trigger angle, so it doesn't flicker on and off at the edge. Tilt is also ignored
while the remote is being swung, so a shake doesn't fire random tilt directions. Untick
*Detect shake and tilt gestures* to turn all of this off for a profile.

Tilt as a continuous analog input, rather than a press, is still available as a stick, mouse
or movement-key source (`wiimote.tilt` / `nunchuk.tilt`).

### Keyboard & mouse mode

Every button row in the **Mapping** tab has two columns: **Xbox controller** and
**Keyboard / mouse**. A button can drive both at once. The **Keyboard & mouse** box sets:

* **Also drive the virtual Xbox controller.** Untick it for pure keyboard/mouse profiles, so a
  game doesn't receive controller input at the same time and switch its button prompts.
* **Mouse pointer source** and **Mouse mode**:
  * *Absolute*: the IR pointer sets the cursor position directly, like aiming a Wii menu. Best for the desktop.
  * *Relative*: a stick, tilt or the IR pointer moves the cursor at a speed set by how far it is pushed. Best for
    games. With the IR pointer, aiming towards the edge of the screen keeps turning the camera.
* **Movement keys**: turns a stick into W/A/S/D or the arrow keys.

Tuning lives in *Settings → Mouse pointer*: speed, deadzone, acceleration curve, smoothing
(steadies IR jitter), pointer range (how much aiming covers the whole screen), axis inversion,
and the movement-key threshold.

| Profile | Mapping |
|---|---|
| **Desktop Mouse (IR pointer)** | Point = cursor, B = left click, A = right click, Nunchuk C = middle click, D-Pad up/down = scroll, D-Pad left/right = back/forward, −/+ = volume, Home = Windows key, 1 = Esc, 2 = Enter, Nunchuk stick = arrow keys, Z = Ctrl |
| **Keyboard & Mouse FPS (Nunchuk WASD)** | Nunchuk stick = WASD, pointer = mouse look, B = fire (left click), A = aim (right click), C = Space (jump), Z = Ctrl (crouch), D-Pad = R / Shift / Q / E, 1/2 = weapon slots, − = Tab, + = Esc, Home = M |

Keys are sent as hardware scan codes, which games that use DirectInput or raw input read. If
several Wiimotes hold the same key, it stays pressed until the last one lets go. When a
Wiimote disconnects, everything it was holding is released immediately.

Existing configurations from version 1.0 receive the two new profiles automatically. Your own
profiles are left untouched.

### Profiles and configuration file

Profiles live in `%APPDATA%\WiimoteXInput\config.json`, together with the log
`wiimote_xinput.log`. Edit them in the **Mapping** and **Settings** tabs. Changes apply instantly,
even mid-game. You can also edit the JSON by hand. Unknown keys or values are ignored, and
out-of-range numbers are clamped.

```json
{
  "name": "My profile",
  "orientation": "vertical",
  "buttons": { "wiimote.A": "A", "wiimote.B": "RT", "nunchuk.Z": "LT", "nunchuk.C": "LB" },
  "left_stick":  { "source": "nunchuk.stick", "deadzone": 0.12, "saturation": 0.95, "curve": 1.0,
                   "invert_x": false, "invert_y": false, "tilt_range_deg": 45 },
  "right_stick": { "source": "ir.pointer", "deadzone": 0.04, "saturation": 0.85 },
  "left_trigger":  { "source": "classic.lt", "deadzone": 0.05 },
  "right_trigger": { "source": "classic.rt", "deadzone": 0.05 },
  "rumble_enabled": true, "rumble_mode": "pwm", "rumble_threshold": 40,
  "xinput_enabled": true,
  "keys": { "wiimote.ONE": "E", "wiimote.TWO": "MOUSE_RIGHT", "wiimote.UP": "WHEEL_UP" },
  "mouse": { "source": "ir.pointer", "mode": "absolute", "speed": 1200, "deadzone": 0.1, "curve": 1.5,
             "invert_x": false, "invert_y": false, "smoothing": 0.5, "absolute_range": 0.7 },
  "key_stick": { "source": "nunchuk.stick", "layout": "wasd", "threshold": 0.5 },
  "motion": { "enabled": true, "shake_threshold_g": 1.0, "shake_hold_s": 0.2, "tilt_angle_deg": 30 }
}
```

Calibration is stored in the same file, separate from the profiles, under `"calibrations"`. It is
keyed by the Wiimote's serial (its Bluetooth address), so it follows the physical remote
whichever profile is active. The Calibration tab is the easiest way to change it.

* Button sources: `wiimote.{UP,DOWN,LEFT,RIGHT,A,B,ONE,TWO,PLUS,MINUS,HOME}`, `nunchuk.{C,Z}`,
  `classic.{UP,DOWN,LEFT,RIGHT,A,B,X,Y,L,R,ZL,ZR,PLUS,MINUS,HOME}`, and the motion gestures
  `motion.{WIIMOTE,NUNCHUK}_{SHAKE,TILT_LEFT,TILT_RIGHT,TILT_UP,TILT_DOWN}`. Gestures work in
  both `buttons` and `keys`.
* Targets: `A B X Y LB RB LT RT BACK START GUIDE LS RS DPAD_UP DPAD_DOWN DPAD_LEFT DPAD_RIGHT NONE`.
* Stick sources (also used for `mouse` and `key_stick`): `none`, `nunchuk.stick`, `classic.left_stick`,
  `classic.right_stick`, `wiimote.tilt`, `nunchuk.tilt`, `ir.pointer`, `wiimote.dpad`.
* Keyboard / mouse targets (`keys`): letters `A`–`Z`, digits `0`–`9`, `F1`–`F12`, `SPACE ENTER ESCAPE TAB
  BACKSPACE LSHIFT RSHIFT LCTRL RCTRL LALT RALT LWIN UP DOWN LEFT RIGHT INSERT DELETE HOME END PAGEUP
  PAGEDOWN CAPSLOCK MINUS EQUALS COMMA PERIOD SLASH SEMICOLON APOSTROPHE LBRACKET RBRACKET BACKSLASH GRAVE`,
  `NUMPAD0`–`NUMPAD9`, `VOLUME_UP VOLUME_DOWN VOLUME_MUTE MEDIA_PLAY_PAUSE MEDIA_NEXT MEDIA_PREV`, and
  `MOUSE_LEFT MOUSE_RIGHT MOUSE_MIDDLE MOUSE_X1 MOUSE_X2 WHEEL_UP WHEEL_DOWN`.

---

## 4. Testing

```powershell
pip install -r requirements-dev.txt
python -m pytest                                      # 65 tests, no hardware needed
python -m wiimote_xinput --simulate --dry-run         # full GUI fed by a simulated Wiimote + Nunchuk
python -m wiimote_xinput --simulate                   # same, but drives a real virtual Xbox pad
python -m wiimote_xinput --list                       # Wiimote HID devices Windows exposes
python -m wiimote_xinput -v                           # debug logging (raw protocol events)
```

Checklist with real hardware:

1. `--list` shows the remote. If it doesn't, the problem is pairing (section 5).
2. The dashboard card shows **Connected** at about 100 reports/s, and the battery reading is plausible.
3. Press buttons and move the Nunchuk. The live Xbox view mirrors them.
4. Open `joy.cpl` or Steam's controller tester and confirm the same there.
5. Use Steam's controller test vibration, or any game. The Wiimote should rumble.
6. Switch the remote off. Within about 6 s the card shows **Reconnecting**, and in-game inputs drop to neutral. Press a button to bring it back on the same virtual pad.
7. Select **Desktop Mouse (IR pointer)** with a sensor bar powered on. Pointing moves the cursor, and B clicks. Open Notepad and confirm 1/2 send Esc/Enter.

---

## 5. Troubleshooting

| Symptom | Cause and fix |
|---|---|
| **Windows asks for a PIN** | Wiimotes have no typeable PIN. Leave it empty, or use **Pair Wiimote…** / `--pair`, which sends the real binary PIN (the host's Bluetooth address when using SYNC, the remote's own address with 1+2). |
| Paired, but the remote never shows up, or LEDs blink then stop | Stale pairing. Remove "Nintendo RVL-CNT-01" in *Bluetooth & devices*, then pair again with the helper and the **SYNC** button. The helper removes stale entries automatically. |
| Has to be re-paired after every reboot | It was paired with 1+2 or with an empty PIN (temporary). Pair once with the **SYNC** button through the helper. |
| Connects, but no input / "no status report" in the log | Some Bluetooth stacks (Toshiba, some Broadcom/Realtek drivers) and Wiimote clones reject `WriteFile`. *Settings → HID write method → HidD_SetOutputReport* (Automatic already falls back), then reconnect. |
| Card says Connected but the buttons do nothing in games | ViGEmBus missing or blocked. Check the banner or `--check-driver`. Also check that Steam Input isn't remapping the pad: *Steam → Settings → Controller → Xbox Configuration Support* off, or configure per game. |
| Double input in a game | The game reads both the virtual pad and something else (for example Steam Input's own virtual pad). Disable Steam Input for that game, or hide the extra device with [HidHide](https://github.com/nefarius/HidHide). |
| Nunchuk / Classic not detected | Re-seat the plug. Third-party extensions that only work in encrypted mode are rare; this app uses the unencrypted init that clones support. Check the log for the extension ID. A remote with built-in MotionPlus (`-TR`) works, but MotionPlus gyro data isn't used. |
| Stick drifts or doesn't reach the edges | *Calibration → Nunchuk stick* (or *Classic Controller*), then *Deadzone helper*. Without calibration, ranges also self-adjust as you move the stick. |
| Pointer doesn't line up with the screen | *Calibration → IR pointer*, then set *Pointer range* to 100%. |
| Tilt reads a few degrees off while lying flat | *Calibration → Wiimote accelerometer*. Old remotes drift from their factory values. |
| Tilt / IR goes the wrong way | Use the **Invert X/Y** toggles for that stick. IR needs a powered sensor bar (or two IR candles) about 1–3 m away. |
| Frequent disconnects | Fresh batteries (below 20% the Wiimote radio gets flaky), keep line of sight, avoid USB 3.0 ports right next to a Bluetooth dongle (2.4 GHz interference), or use a DolphinBar in mode 4. |
| Shake fires too easily, or not at all | Raise or lower *Shake strength needed* in *Settings → Motion gestures*. Around 0.6 g catches gentle waggles; 1.5 g or more needs a hard swing. |
| Tilt left/right are swapped for how you hold it | Swap the two bindings. Tilt directions follow the remote's own axes, as shown live on the dashboard ("Wiimote tilt right"). |
| Keyboard/mouse does nothing in one window, works elsewhere | Windows blocks injected input into programs running **as administrator** (UIPI). Run Wiimote → XInput as administrator too, or run the target program normally. |
| Keys work on the desktop but a game ignores them | Some anti-cheat systems reject injected input. Use the Xbox controller output for those games. |
| Relative mouse feels floaty or inconsistent | Turn off *Enhance pointer precision* (Windows mouse settings), then adjust speed and curve in *Settings → Mouse pointer*. |
| Absolute cursor jitters | Increase *Smoothing*. Move closer to the sensor bar, or lower the *Pointer range* so smaller wrist movements cover the screen. |
| Typing a game's keys also triggers the controller | In that profile, untick *Also drive the virtual Xbox controller*. |
| "Already running" message | Another instance is in the tray. Two instances would fight over the same remote. |
| Bluetooth adapter not found by the helper | Turn Bluetooth on. Some adapters only expose their radio after the vendor driver is installed. |

Logs: `%APPDATA%\WiimoteXInput\wiimote_xinput.log` (use `-v` for protocol-level detail).
