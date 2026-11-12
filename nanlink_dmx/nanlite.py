"""Nanlite vendor-model commands (company 0x1111, opcode C1 11 11).

Every command is an 8-byte parameter block:

    rCode | funcCode | typeCode | optionCode | value (BE u16) | channel (BE u16)

Confirmed with the WS-TB-1 (captured from the Nanlink app and tested):
    funcCode 0x28 = SET, routed to 2.4 GHz fixtures (lightType = 1)
    typeCode 0x01 / option 0x01 = brightness, value 0-100, channel = Nanlink channel

Other options come from reverse-engineering of BLE fixtures and are not yet
verified through the transmitter box.
"""

from __future__ import annotations

import itertools
import struct

VENDOR_OPCODE = bytes([0xC1, 0x11, 0x11])

FUNC_SET = 0x20
FUNC_QUERY = 0x01
LIGHT_TYPE_24G = 0x08          # funcCode bits 4-3 = 01
FUNC_SET_24G = FUNC_SET | LIGHT_TYPE_24G

TYPE_CONTROL = 0x01
TYPE_SET_MODE = 0x09
TYPE_QUERY_ALL = 0x0C

OPT_DIM = 0x01
OPT_CCT = 0x03
OPT_GM = 0x04
OPT_HUE = 0x05
OPT_SAT = 0x0C

MODE_CCT = 2
MODE_HSI = 3

_rcode = itertools.cycle(range(8))


def command(func: int, type_code: int, option: int, value: int, channel: int) -> bytes:
    """Full access PDU (opcode + params)."""
    return VENDOR_OPCODE + struct.pack(">BBBBHH", next(_rcode), func, type_code, option,
                                       value & 0xFFFF, channel & 0xFFFF)


def set_param(option: int, value: int, channel: int) -> bytes:
    return command(FUNC_SET_24G, TYPE_CONTROL, option, value, channel)


def set_mode(mode: int, channel: int) -> bytes:
    return command(FUNC_SET_24G, TYPE_SET_MODE, mode, 0, channel)


def register_channel(channel: int) -> list[bytes]:
    """Tell the box a 2.4 GHz light exists on `channel`.

    Replayed from what the Nanlink app sends when a scene opens; a freshly
    reset box ignores brightness commands for channels it hasn't been told
    about. The meaning of the individual bytes is unknown (0x0101 values were
    captured with single-colour white lights)."""
    ch = struct.pack(">H", channel)
    return [
        VENDOR_OPCODE + bytes([next(_rcode), FUNC_SET, TYPE_CONTROL, 0x5E, 0x01, 0x01]) + ch,
        VENDOR_OPCODE + bytes([next(_rcode), 0x2C]) + ch + bytes.fromhex("0101010500000101"),
    ]


def query_box() -> bytes:
    return command(FUNC_QUERY, TYPE_QUERY_ALL, 0, 0, 0)
