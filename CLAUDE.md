# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

**Gran2Nan** (formerly NanlinkBridge; the Python package is still `nanlink_dmx`, and the app name lives in `APP_NAME` in `nanlink_dmx/__init__.py`): an Art-Net → Bluetooth Mesh bridge that drives Nanlite lights through a NANLINK WS-TB-1 transmitter box. Python 3.10+, Tkinter GUI, `bleak` for BLE, `cryptography` for mesh crypto. `README.md` is the user manual and has the protocol notes. Read it before you change protocol behaviour.

## Commands (Windows, from the repo root)

```
.venv\Scripts\python -m pytest tests                                   # all tests
.venv\Scripts\python -m pytest tests/test_bridge.py::test_personalities # one test
.venv\Scripts\python Gran2Nan.pyw                                  # GUI
.venv\Scripts\python -m nanlink_dmx --headless                          # no window, uses config.json
build_exe.bat                                                            # PyInstaller onefile → dist\Gran2Nan.exe
```

`run.bat` creates the venv on first use and starts the GUI. There is no linter config. Not a git repo.

## Architecture

Data flows: `artnet.py` (UDP 6454, DmxFrame) → `engine.Bridge` → `fixtures.py` personality maps DMX slots to `{param: (option, value)}` targets → `nanlite.py` encodes a vendor access PDU (opcode `C1 11 11`) → `link.MeshLink` → `mesh.py` encrypts it into network PDUs with proxy framing → GATT write to the box.

- **`engine.Bridge`** is the core. It arbitrates control (Art-Net if a frame arrived within `ARTNET_TIMEOUT`, else manual faders, or manual-only mode). It also does change detection per fixture param and runs one `_sender` task. The sender is rate-limited to `output.max_rate`, always sends the *latest* value, and resends settled values once (`SETTLE_DELAY`). The engine reports to the UI only through the `emit(kind, data)` callback (`"log"`, `"status"`, …). The GUI and headless mode are both just `emit` consumers.
- **Threading:** all of the bridge's asyncio runs on a dedicated event-loop thread created in `gui.py`. Tk calls go into it with `run_coroutine_threadsafe`. Events come back through a `queue.Queue` that Tk polls with `after(50, …)`. Never touch Tk widgets from the asyncio thread.
- **`link.MeshLink`** scans for the box, connects, sends the Feasycom handshake (`$FSCCMD001$\r\n` on `0xFFF2`), writes to the proxy characteristic `0x2ADD`, and reconnects automatically. On every connect the engine replays the channel registration (option `0x5E` + `0x2C` packet). Without it a factory-reset box ignores dimming.
- **`provision.py`** has "Pair box…": PB-GATT provisioning with a random NetKey, followed by config (AppKey add/bind). The AppKey **must** be Nanlink's fixed `6396…4A48`. The box accepts any other key but then silently ignores vendor commands.
- **`keyimport.py`** is the alternative to pairing. It parses mesh keys from the Nanlink Android app's logcat via adb.
- **`config.py`**: `config.json` sits next to the exe/repo and holds the mesh keys. It is gitignored, so never print or commit its keys. If that folder is read-only, it falls back to `%LOCALAPPDATA%\Gran2Nan`. `SeqStore` keeps a **per-machine** source address and sequence counter in `%LOCALAPPDATA%\Gran2Nan\state.json`, reserved in blocks of 1000. Mesh replay protection drops any seq that is not higher than the last one, so never reset or reuse it, and never move this state into the shareable config.
- **About tab** (`_build_about_tab` in `gui.py`): the app overview and the credits (OddLux, info@oddlux.lighting, GitHub). The repo is public: keep personal data out of it.
- **Adding a fixture type:** add a `Personality` to `PERSONALITIES` in `fixtures.py`.

## Hardware facts and constraints

- Verified on hardware: the dimmer only (`rc 28 01 01 <pct BE16> <ch BE16>`, 0–100 %). CCT and HSI are experimental and unverified through the box. Brightness has 101 steps.
- `tests/test_mesh.py` checks the mesh layer against the Mesh Profile 1.0 spec sample data. Keep those passing when you edit `mesh.py`.
- Only one client (bridge or the phone app) can be connected to the box at a time. Hardware behaviour can't be tested from here, so tell the user what needs a manual check on the real lights.
- Default `max_rate` is 20 msg/s and has not been stress-tested. Ask before you assume higher rates work.
- `dist/` and `portable/` hold built outputs (exe + config + README). Rebuild them; don't hand-edit them.
