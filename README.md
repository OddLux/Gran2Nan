# Gran2Nan

**Console talk Art-Net. Nanlite light no hear Art-Net. Gran2Nan fix.**

```
Console ──Art-Net──▶ Gran2Nan (PC) ──Bluetooth Mesh──▶ WS-TB-1 ──2.4 GHz──▶ lights (ch 1, 2, …)
```

- Console (grandMA3, any Art-Net desk) shout DMX on network.
- Gran2Nan on PC hear. Gran2Nan turn DMX into Nanlite words.
- Gran2Nan whisper Bluetooth to **NANLINK WS-TB-1** box.
- Box yell 2.4 GHz to every light on its Nanlink channel. Light go bright. Light go dark.
- No console? Faders in app. Human push fader. Light obey.
- One exe. No install. Put on USB stick. Carry to other PC.

Below: the long words, for human who read.

## Running it

**Any Windows 10/11 PC, no install needed:** download `Gran2Nan.exe` from
[Releases](../../releases) (or build it: `dist\Gran2Nan.exe`), put it and your
`config.json` in the same folder and start the exe. The settings stay in that folder, so you can
carry it on a USB stick. If Windows SmartScreen warns about an unknown publisher, click
*More info → Run anyway*. The exe is not code-signed.

**From source (Windows, macOS, Linux):** Python 3.10+, then `run.bat`, or:

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python Gran2Nan.pyw            # GUI
.venv\Scripts\python -m nanlink_dmx --headless    # no window, uses config.json
```

**Rebuilding the exe:** `build_exe.bat` creates `dist\Gran2Nan.exe`.

The PC needs Bluetooth (built-in or a USB dongle). Keep it within normal Bluetooth range
(roughly 10 m) of the WS-TB-1. The box only needs USB power; its USB port carries no data.

## First-time setup

The WS-TB-1 only accepts commands encrypted with the keys of its Bluetooth Mesh network. There
are two ways to get them. Either way you do it once, on one PC. Every other PC gets the keys
through *Export settings / Import settings*.

### Option A: let the bridge pair the box (no phone, recommended)

1. Factory-reset the WS-TB-1 and close the Nanlink app on your phone.
2. **Settings → Pair box…**. The bridge pairs the box with a new private network key and saves
   everything.
3. Continue with step 4 below (fixtures).

After this the Nanlink app can't control the box. To go back, reset the box and add it in the
app. To re-pair with the bridge later, reset the box and click *Pair box…* again.

### Option B: import the Nanlink app's keys (keeps the app working)

1. On an **Android** phone with the Nanlink app, enable *Developer options → USB debugging* and
   connect the phone to the PC by USB. adb is needed:
   `winget install Google.PlatformTools`.
2. Fully close the Nanlink app, reopen it, open the scene with the WS-TB-1, and change a light
   once.
3. In the bridge go to **Settings → Import keys from phone (adb)**, accept the USB debugging
   prompt on the phone if one appears, then click **Save & apply**.
   - This fills in the NetKey, AppKey, IV index and the box's mesh address. It also fills in the
     box's Bluetooth address if that field is empty. **Scan** lists nearby boxes.
   - *Keys OK · network ID …* confirms the keys. If the box later says it *belongs to a
     different mesh network*, the box was re-paired in the Nanlink app: import again.
4. **Fixtures & Control → Add…** for each light:
   - **Nanlink channel**: the channel set on the light.
   - **DMX start address**: where you patch it on the console.
   - **Type**: the channel layout.
5. **Settings → Art-Net**: pick the universe. Most consoles call Art-Net `0:0:0` "Universe 1".
   *Listen on 0.0.0.0* accepts all network adapters. Allow the app through the Windows Firewall
   when asked (private networks). Otherwise Art-Net from other machines is blocked.

**Moving to another PC:** use *Settings → Export settings…*, then on the other PC *Import
settings…*, or copy `config.json` next to the exe. The file contains the mesh keys, so keep it
private. Each PC automatically uses its own mesh address and message counter, stored in
`%LOCALAPPDATA%\Gran2Nan\state.json`. This way the box never rejects a PC because another one
used the bridge before it.

## Fixture types (DMX personalities)

| Type | DMX channels | Status |
|---|---|---|
| Dimmer | 1: intensity 0-255 → 0-100 % | Tested via the WS-TB-1 |
| CCT | 1: intensity, 2: CCT (range set per fixture, default 2700-6500 K), 3: G/M (128 = neutral) | Experimental |
| HSI | 1: intensity, 2: hue 0-360°, 3: saturation | Experimental |

The CCT and HSI types use commands documented for Nanlite's Bluetooth lights. They have not been
verified through the transmitter box yet. New types are a few lines in `nanlink_dmx/fixtures.py`.
Brightness has 101 steps (whole percent); that is a limit of the Nanlite protocol.

## Who is in control

- **Art-Net arriving** (within the last 3 s): the console controls the lights, and the faders in
  the app follow it.
- **No Art-Net**: the lights hold their last state, and the faders in the app control them. As
  soon as Art-Net arrives again, the console takes over.
- **Manual only**: the console is ignored completely.
- **Identify** flashes the selected light. **All full / All off** set every dimmer.

Only one program can be connected to the WS-TB-1 at a time. Close the Nanlink app on the phone,
or the bridge, before using the other.

## Output tuning (Settings → Output)

- **Max messages / second** (default 20): each changed parameter of each light is one Bluetooth
  message. The bridge always sends the latest value and skips intermediate ones, so fast fades
  stay in sync but get coarser with many lights. Raise it if the box keeps up.
- **Repeat each final value once**: the 2.4 GHz link has no acknowledgements. Sending the
  resting value a second time makes it reliable, as the Nanlink app does.
- **Refresh all every N s**: optionally re-sends everything periodically, e.g. for lights that
  get switched on later.

## Troubleshooting

| Symptom | Fix |
|---|---|
| *Bluetooth: scanning* forever | Box powered? Within range? Nanlink app still connected to it? Click **Scan** in Settings. |
| *belongs to a different mesh network* | The box was re-paired in the Nanlink app. Import keys again. |
| Connected, but lights don't react | Check the Nanlink channel on the light and the *Box mesh address* (from the import). |
| *Art-Net: no data (seen universe …)* | The console sends a different universe. Change it in Settings. |
| *cannot open Art-Net port 6454* | Another Art-Net program on this PC uses the port. Close it, or pick a specific *Listen on* address. |

## How it works (protocol notes)

- The WS-TB-1 advertises as `WS_TB_1` and is a **Bluetooth Mesh GATT proxy** (FeasyMesh SDK).
  The bridge connects, sends the Feasycom handshake `$FSCCMD001$\r\n` on `0xFFF2`, then sends
  encrypted mesh network PDUs to `0x2ADD`. The mesh layer follows the Mesh Profile 1.0 and is
  checked against the spec's sample data in `tests/`.
- Light commands are vendor messages, opcode `C1 11 11`, to the box's unicast address. The 8
  parameter bytes are `rCode, funcCode, typeCode, option, value(BE16), channel(BE16)`. For
  example, 50 % on channel 1 is `xx 28 01 01 0032 0001`. funcCode `0x28` means "set, via
  2.4 GHz". This was captured from the Nanlink app and verified on hardware.
- The box only executes vendor commands encrypted with the AppKey the Nanlink app uses
  everywhere (`6396…4A48`). A random AppKey is accepted and bound, but light commands are then
  silently ignored. Pairing therefore uses a random NetKey with that fixed AppKey.
- A reset box ignores brightness for channels it hasn't been told about. On every connect, the
  bridge replays the Nanlink app's channel registration (option `0x5E` and a `0x2C` packet) for
  each patched channel. The registration bytes were captured with single-colour lights; other
  fixture types may need different values.
- Mesh replay protection drops any message whose sequence number is not higher than the last one
  seen from the same sender. That is why the counter is persisted per PC.

## Files

| Path | Purpose |
|---|---|
| `nanlink_dmx/mesh.py` | Bluetooth Mesh crypto, network/transport PDUs, proxy framing |
| `nanlink_dmx/link.py` | BLE connection to the box, auto-reconnect |
| `nanlink_dmx/nanlite.py` | Nanlite vendor command encoding |
| `nanlink_dmx/fixtures.py` | DMX personalities |
| `nanlink_dmx/artnet.py` | Art-Net receiver + ArtPollReply (console discovery) |
| `nanlink_dmx/engine.py` | Control arbitration, change detection, rate-limited sending |
| `nanlink_dmx/keyimport.py` | Reads mesh keys from the Nanlink app's Android log |
| `nanlink_dmx/gui.py` | Desktop UI |
| `tests/` | `python -m pytest tests` |
