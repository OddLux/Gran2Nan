"""The bridge: Art-Net / manual faders -> fixture parameters -> rate-limited mesh messages."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

from . import nanlite
from .artnet import ArtNetReceiver, DmxFrame, start_receiver, universe_label
from .config import Config, FixtureConfig, SeqStore
from .fixtures import PERSONALITIES, Personality
from .link import CONNECTED, MeshLink

log = logging.getLogger("nanlink.engine")

ARTNET_TIMEOUT = 3.0     # seconds without frames before Art-Net counts as gone
SETTLE_DELAY = 0.25      # resend a value this long after it stopped changing
BURST_GAP = 0.005        # pause between back-to-back messages in a burst


@dataclass
class FixtureState:
    cfg: FixtureConfig
    personality: Personality
    dmx: bytearray                                          # current slot values (footprint)
    sent: dict[str, int] = field(default_factory=dict)      # param -> last value sent
    mode_sent: bool = False

    def targets(self) -> dict[str, tuple[int, int]]:
        opts = dict(self.personality.default_options)
        opts.update(self.cfg.options or {})
        return self.personality.map(bytes(self.dmx), opts)


class Bridge:
    def __init__(self, cfg: Config, emit: Callable[[str, object], None] = lambda kind, data: None):
        self.cfg = cfg
        self.emit = emit
        self.link: MeshLink | None = None
        self.receiver: ArtNetReceiver | None = None
        self._transport = None
        self.fixtures: list[FixtureState] = []
        self.manual_only = False
        self._dirty: OrderedDict[tuple[int, str], None] = OrderedDict()
        self._settle: dict[tuple[int, str], float] = {}
        self._direct: asyncio.Queue[bytes] = asyncio.Queue()
        self._wake = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._seq: SeqStore | None = None
        self._last_refresh = time.monotonic()
        self._sent_window: list[float] = []
        self.running = False
        self._build_fixtures()

    # ---- setup -----------------------------------------------------------

    def _build_fixtures(self):
        old = {(f.cfg.channel, f.cfg.dmx_address, f.cfg.personality): f for f in self.fixtures}
        self.fixtures = []
        for fc in self.cfg.fixtures:
            p = PERSONALITIES.get(fc.personality)
            if not p:
                log.error("unknown fixture type %r for %s", fc.personality, fc.name)
                continue
            prev = old.get((fc.channel, fc.dmx_address, fc.personality))
            st = FixtureState(fc, p, prev.dmx if prev else bytearray(p.footprint))
            if prev:
                st.sent, st.mode_sent = prev.sent, prev.mode_sent
            self.fixtures.append(st)
        self._dirty.clear()
        self._settle.clear()
        self._mark_all_dirty()

    async def start(self):
        if self.running:
            return
        self.running = True
        await self._start_artnet()
        self._start_link()
        self._tasks = [asyncio.create_task(self._sender()), asyncio.create_task(self._status_loop())]

    async def stop(self):
        self.running = False
        for t in self._tasks:
            t.cancel()
        self._tasks = []
        if self._transport:
            self._transport.close()
            self._transport = None
        if self.link:
            await self.link.stop()
            self.link = None
        self._emit_status()

    async def apply_config(self, cfg: Config):
        old = self.cfg
        self.cfg = cfg
        self._build_fixtures()
        if self.link and self.link.connected:
            new = {f.channel for f in cfg.fixtures if f.enabled} - {f.channel for f in old.fixtures if f.enabled}
            for ch in sorted(new):
                for pdu in nanlite.register_channel(ch):
                    self._direct.put_nowait(pdu)
        if not self.running:
            self._emit_status()
            return
        if (old.mesh, old.ble) != (cfg.mesh, cfg.ble) or self.link is None:
            if self.link:
                await self.link.stop()
            self._start_link()
        if old.artnet != cfg.artnet:
            if self._transport:
                self._transport.close()
            await self._start_artnet()
        self._wake.set()

    async def _start_artnet(self):
        a = self.cfg.artnet
        self.receiver = ArtNetReceiver(a.universe, self._on_frame, a.short_name, self._node_report)
        try:
            self._transport = await start_receiver(a.bind_ip, self.receiver)
            self.emit("log", f"Art-Net listening on {a.bind_ip}:6454, universe {universe_label(a.universe)}")
        except OSError as e:
            self._transport = None
            self.emit("log", f"ERROR: cannot open Art-Net port 6454 on {a.bind_ip}: {e}")

    def seq_store(self) -> SeqStore:
        m = self.cfg.mesh
        if self._seq is None or self._seq.configured_src != m.src_address:
            self._seq = SeqStore(m.src_address, avoid=(m.box_address, 0x0001))
        return self._seq

    async def release_link(self):
        """Disconnect from the box (e.g. while pairing) until apply_config/start reconnects."""
        if self.link:
            await self.link.stop()
            self.link = None

    def _start_link(self):
        m = self.cfg.mesh
        if not (m.net_key and m.app_key):
            self.link = None
            self.emit("log", "Mesh keys are not set - open Settings and import them from the Nanlink app")
            return
        try:
            self.link = MeshLink(m, self.cfg.ble.device_address, self.cfg.ble.device_name, self.seq_store(),
                                 on_state=self._on_link_state, on_access=self._on_access)
        except ValueError as e:
            self.link = None
            self.emit("log", f"ERROR: invalid mesh keys: {e}")
            return
        self.link.start()

    # ---- inputs ----------------------------------------------------------

    @property
    def artnet_live(self) -> bool:
        return bool(self.receiver) and time.monotonic() - self.receiver.last_frame_time < ARTNET_TIMEOUT

    @property
    def control_source(self) -> str:
        if self.manual_only:
            return "manual"
        return "artnet" if self.artnet_live else "manual"

    def _on_frame(self, frame: DmxFrame):
        if self.manual_only:
            return
        for i, f in enumerate(self.fixtures):
            if not f.cfg.enabled:
                continue
            start = f.cfg.dmx_address - 1
            new = frame.data[start:start + f.personality.footprint]
            if new != f.dmx:
                f.dmx[:] = new
                self._update(i)

    def set_manual(self, index: int, slot: int, value: int):
        """Fader moved in the app. Ignored while the console is in control."""
        if self.control_source != "manual" or not 0 <= index < len(self.fixtures):
            return
        f = self.fixtures[index]
        if 0 <= slot < len(f.dmx) and f.dmx[slot] != value:
            f.dmx[slot] = max(0, min(255, int(value)))
            self._update(index)

    def set_manual_only(self, on: bool):
        self.manual_only = on
        self.emit("log", "Manual only: console input ignored" if on else "Console input enabled")
        self._emit_status()

    def identify(self, index: int):
        if 0 <= index < len(self.fixtures):
            asyncio.create_task(self._identify(self.fixtures[index], index))

    async def _identify(self, f: FixtureState, index: int):
        for level in (100, 0, 100, 0, 100, 0):
            self._direct.put_nowait(nanlite.set_param(nanlite.OPT_DIM, level, f.cfg.channel))
            self._wake.set()
            await asyncio.sleep(0.4)
        f.sent.pop("dim", None)
        self._update(index)

    def _update(self, index: int):
        f = self.fixtures[index]
        if not f.cfg.enabled:
            return
        for param, (_, value) in f.targets().items():
            key = (index, param)
            if f.sent.get(param) != value:
                self._dirty[key] = None  # keeps its queue position if already pending
            elif key in self._dirty:
                del self._dirty[key]
        self._wake.set()

    def _mark_all_dirty(self):
        for i, f in enumerate(self.fixtures):
            f.sent.clear()
            f.mode_sent = False
            self._update(i)

    # ---- output ----------------------------------------------------------

    def _on_link_state(self, state: str, detail: str):
        self.emit("link", (state, detail))
        if state == CONNECTED:
            self.emit("log", f"Connected to WS-TB-1 ({detail})")
            for ch in sorted({f.cfg.channel for f in self.fixtures if f.cfg.enabled}):
                for pdu in nanlite.register_channel(ch):
                    self._direct.put_nowait(pdu)
            self._mark_all_dirty()  # lights may have been changed elsewhere; resend everything
        elif state == "disconnected" and detail:
            self.emit("log", f"Bluetooth: {detail}")

    def _on_access(self, src: int, access: bytes):
        self.emit("log", f"<- 0x{src:04X}: {access.hex(' ')}")

    def _next_message(self) -> bytes | None:
        if not self._direct.empty():
            return self._direct.get_nowait()
        now = time.monotonic()
        while self._dirty:
            (index, param), _ = self._dirty.popitem(last=False)
            if index >= len(self.fixtures):
                continue
            f = self.fixtures[index]
            if f.personality.mode is not None and not f.mode_sent:
                f.mode_sent = True
                self._dirty[(index, param)] = None
                self._dirty.move_to_end((index, param), last=False)
                return nanlite.set_mode(f.personality.mode, f.cfg.channel)
            targets = f.targets()
            if param not in targets:
                continue
            option, value = targets[param]
            f.sent[param] = value
            if self.cfg.output.settle_repeat:
                self._settle[(index, param)] = now + SETTLE_DELAY
            return nanlite.set_param(option, value, f.cfg.channel)
        # Nothing new: resend values that have stopped changing
        for key, due in list(self._settle.items()):
            if due <= now:
                del self._settle[key]
                index, param = key
                if index < len(self.fixtures) and param in self.fixtures[index].sent:
                    f = self.fixtures[index]
                    option, value = f.targets()[param]
                    if value == f.sent[param]:
                        return nanlite.set_param(option, value, f.cfg.channel)
        refresh = self.cfg.output.refresh_interval
        if refresh > 0 and now - self._last_refresh >= refresh:
            self._last_refresh = now
            for i, f in enumerate(self.fixtures):
                f.sent.clear()
                self._update(i)
        return None

    def _next_wakeup(self) -> float | None:
        if self._settle:
            return max(0.0, min(self._settle.values()) - time.monotonic())
        refresh = self.cfg.output.refresh_interval
        if refresh > 0:
            return max(0.0, self._last_refresh + refresh - time.monotonic())
        return None

    async def _sender(self):
        # Token bucket: a cue that changes several lights goes out as one burst so they react
        # together; max_rate still limits the average rate.
        tokens, last = float(self.cfg.output.burst), time.monotonic()
        while True:
            link = self.link
            if not link or link.state != CONNECTED:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass
                continue
            msg = self._next_message()
            if msg is None:
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), self._next_wakeup())
                except asyncio.TimeoutError:
                    pass
                continue
            if await link.send_access(msg):
                self._sent_window.append(time.monotonic())
            rate, burst = max(1.0, self.cfg.output.max_rate), max(1, self.cfg.output.burst)
            now = time.monotonic()
            tokens = min(float(burst), tokens + (now - last) * rate) - 1.0
            last = now
            await asyncio.sleep(BURST_GAP if tokens >= 1.0 else (1.0 - tokens) / rate)

    # ---- status ----------------------------------------------------------

    def _node_report(self) -> str:
        return "BLE connected" if self.link and self.link.connected else "BLE not connected"

    async def _status_loop(self):
        while True:
            self._emit_status()
            await asyncio.sleep(0.2)

    def _emit_status(self):
        now = time.monotonic()
        self._sent_window = [t for t in self._sent_window if now - t < 1.0]
        r = self.receiver
        self.emit("status", {
            "running": self.running,
            "link": self.link.state if self.link else "not configured",
            "mesh_src": self._seq.src if self._seq else None,
            "artnet_live": self.artnet_live,
            "artnet_source": r.last_source if r else "",
            "artnet_frames": r.frames if r else 0,
            "artnet_other": sorted(r.other_universes) if r else [],
            "universe_data": r.last_data if r else bytes(512),
            "control": self.control_source,
            "manual_only": self.manual_only,
            "rate": len(self._sent_window),
            "pending": len(self._dirty),
            "fixtures": [(bytes(f.dmx), dict(f.sent)) for f in self.fixtures],
        })
