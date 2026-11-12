"""Configuration and persistent state (JSON files next to the app)."""

from __future__ import annotations

import json
import os
import random
import sys
import threading
from dataclasses import asdict, dataclass, field


def _writable(path: str) -> bool:
    try:
        probe = os.path.join(path, ".write_test")
        with open(probe, "w"):
            pass
        os.remove(probe)
        return True
    except OSError:
        return False


def _app_dir() -> str:
    """config.json lives next to the program so the folder can be copied to another PC;
    if that folder is read-only (e.g. Program Files) it goes to the user's profile."""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return base if _writable(base) else _machine_dir()


def _machine_dir() -> str:
    """Per-computer data that must never be copied between machines."""
    root = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), ".local", "share")
    path = os.path.join(root, "Gran2Nan")
    old = os.path.join(root, "NanlinkBridge")  # folder name before the rename: keep its mesh sequence counter
    if not os.path.exists(path) and os.path.isdir(old):
        try:
            os.rename(old, path)
        except OSError:
            return old
    os.makedirs(path, exist_ok=True)
    return path


APP_DIR = _app_dir()
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
STATE_PATH = os.path.join(_machine_dir(), "state.json")


@dataclass
class MeshConfig:
    net_key: str = ""
    app_key: str = ""
    device_key: str = ""       # only known when the bridge paired the box itself
    iv_index: int = 0
    box_address: int = 0x0002   # unicast address of the WS-TB-1 in the mesh
    src_address: int = 0       # our own unicast address; 0 = pick one per computer automatically
    ttl: int = 5


@dataclass
class BleConfig:
    device_address: str = ""   # e.g. 00:11:22:33:44:55
    device_name: str = "WS_TB_1"


@dataclass
class ArtNetConfig:
    bind_ip: str = "0.0.0.0"
    universe: int = 0          # 15-bit Art-Net port-address (net << 8 | subnet << 4 | universe)
    short_name: str = "Gran2Nan"


@dataclass
class OutputConfig:
    max_rate: float = 20.0         # mesh messages per second (average)
    burst: int = 8                 # messages sent back-to-back when several lights change at once
    settle_repeat: bool = True     # resend a value once it stops changing (2.4G link has no acks)
    refresh_interval: float = 0.0  # seconds; periodically resend all values (0 = off)
    autostart: bool = True


@dataclass
class FixtureConfig:
    name: str = "Light"
    channel: int = 1           # Nanlink channel set on the light
    dmx_address: int = 1       # 1-based DMX start address
    personality: str = "dimmer"
    enabled: bool = True
    options: dict = field(default_factory=dict)


@dataclass
class Config:
    mesh: MeshConfig = field(default_factory=MeshConfig)
    ble: BleConfig = field(default_factory=BleConfig)
    artnet: ArtNetConfig = field(default_factory=ArtNetConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    fixtures: list[FixtureConfig] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        def build(dc, data):
            known = {f for f in dc.__dataclass_fields__}
            return dc(**{k: v for k, v in (data or {}).items() if k in known})

        return cls(
            mesh=build(MeshConfig, d.get("mesh")),
            ble=build(BleConfig, d.get("ble")),
            artnet=build(ArtNetConfig, d.get("artnet")),
            output=build(OutputConfig, d.get("output")),
            fixtures=[build(FixtureConfig, f) for f in d.get("fixtures", [])],
        )

    def copy(self) -> "Config":
        return Config.from_dict(json.loads(json.dumps(self.to_dict())))


def load_config(path: str = CONFIG_PATH) -> Config:
    if not os.path.exists(path):
        return Config()
    with open(path, encoding="utf-8") as f:
        return Config.from_dict(json.load(f))


def save_config(cfg: Config, path: str = CONFIG_PATH) -> None:
    _atomic_write(path, json.dumps(cfg.to_dict(), indent=2))


def _atomic_write(path: str, text: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


AUTO_SRC_RANGE = (0x7000, 0x7EFF)


class SeqStore:
    """Mesh sequence numbers must never repeat for a given source address,
    or the box's replay protection silently drops our messages.

    Each computer gets its own source address (stored in the per-machine data
    folder, never next to the shareable config), so several PCs can drive the
    same box. The counter is persisted in blocks: on disk we always store a
    value at or above anything we may have used, so a crash only skips ahead.
    When the 24-bit space runs low we switch to a new source address, which
    gives a fresh replay-protection entry on the box."""

    BLOCK = 1000
    LIMIT = 0xFFF000

    def __init__(self, configured_src: int = 0, path: str = STATE_PATH, avoid: tuple[int, ...] = ()):
        self.path = path
        self.configured_src = configured_src
        self.avoid = set(avoid)
        self._lock = threading.Lock()
        state = {}
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    state = json.load(f)
            except (OSError, ValueError):
                state = {}
        if state.get("configured_src", 0) == configured_src and "src" in state:
            self.src, self.seq = int(state["src"]), int(state["reserved"])
        elif configured_src:
            # 0x40000 leaves room for anything used during setup/testing with this address
            self.src, self.seq = configured_src, 0x40000
        else:
            self.src, self.seq = self._random_src(), 0
        self._reserved = self.seq
        self._reserve()

    def _random_src(self) -> int:
        while True:
            src = random.randint(*AUTO_SRC_RANGE)
            if src not in self.avoid:
                return src

    def _reserve(self):
        self._reserved = self.seq + self.BLOCK
        _atomic_write(self.path, json.dumps(
            {"configured_src": self.configured_src, "src": self.src, "reserved": self._reserved}))

    def next(self) -> tuple[int, int]:
        """Returns (src, seq) for the next outgoing network PDU."""
        with self._lock:
            if self.seq >= self.LIMIT:
                self.avoid.add(self.src)
                self.src, self.seq = self._random_src(), 0
                self._reserve()
            self.seq += 1
            if self.seq >= self._reserved:
                self._reserve()
            return self.src, self.seq
