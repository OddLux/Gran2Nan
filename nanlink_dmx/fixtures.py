"""DMX personalities: how a fixture's DMX footprint maps to Nanlite parameters.

To add a fixture type, write a function that turns the fixture's DMX slots
into {param: (optionCode, value)} and register it in PERSONALITIES.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import nanlite


def pct(v: int) -> int:
    return round(v * 100 / 255)


def scale(v: int, lo: int, hi: int) -> int:
    return round(lo + (hi - lo) * v / 255)


Targets = dict[str, tuple[int, int]]


@dataclass(frozen=True)
class Personality:
    key: str
    label: str
    channels: tuple[str, ...]
    map: Callable[[bytes, dict], Targets]
    mode: int | None = None      # light mode to select before sending parameters
    experimental: bool = False
    default_options: tuple[tuple[str, object], ...] = ()

    @property
    def footprint(self) -> int:
        return len(self.channels)


def _dimmer(d: bytes, o: dict) -> Targets:
    return {"dim": (nanlite.OPT_DIM, pct(d[0]))}


def _cct(d: bytes, o: dict) -> Targets:
    lo, hi = int(o.get("cct_min", 2700)), int(o.get("cct_max", 6500))
    gm = scale(d[2], -100, 100)
    return {
        "dim": (nanlite.OPT_DIM, pct(d[0])),
        "cct": (nanlite.OPT_CCT, round(scale(d[1], lo, hi) / 10) * 10),
        "gm": (nanlite.OPT_GM, gm & 0xFFFF),
    }


def _hsi(d: bytes, o: dict) -> Targets:
    return {
        "dim": (nanlite.OPT_DIM, pct(d[0])),
        "hue": (nanlite.OPT_HUE, scale(d[1], 0, 360)),
        "sat": (nanlite.OPT_SAT, pct(d[2]) << 8),
    }


PERSONALITIES: dict[str, Personality] = {p.key: p for p in (
    Personality("dimmer", "Dimmer (1 ch)", ("Dimmer",), _dimmer),
    Personality("cct", "CCT: Dim, CCT, G/M (3 ch)", ("Dimmer", "CCT", "G/M"), _cct,
                mode=nanlite.MODE_CCT, experimental=True,
                default_options=(("cct_min", 2700), ("cct_max", 6500))),
    Personality("hsi", "HSI: Dim, Hue, Sat (3 ch)", ("Dimmer", "Hue", "Saturation"), _hsi,
                mode=nanlite.MODE_HSI, experimental=True),
)}


def describe_value(param: str, value: int) -> str:
    if param == "dim":
        return f"{value}%"
    if param == "cct":
        return f"{value}K"
    if param == "gm":
        return f"{value - 0x10000 if value & 0x8000 else value:+d}"
    if param == "hue":
        return f"{value}°"
    if param == "sat":
        return f"{value >> 8}%"
    return str(value)
