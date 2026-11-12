"""Minimal Art-Net 4 node: receives ArtDmx and answers ArtPoll."""

from __future__ import annotations

import asyncio
import socket
import struct
import time
from dataclasses import dataclass
from typing import Callable

ARTNET_PORT = 6454
HEADER = b"Art-Net\x00"
OP_POLL = 0x2000
OP_POLL_REPLY = 0x2100
OP_DMX = 0x5000


@dataclass
class DmxFrame:
    universe: int      # 15-bit port-address
    sequence: int
    data: bytes        # always 512 bytes, zero padded
    source: str


def parse_packet(pkt: bytes) -> tuple[int, bytes] | None:
    if len(pkt) < 10 or pkt[:8] != HEADER:
        return None
    return struct.unpack_from("<H", pkt, 8)[0], pkt


def parse_dmx(pkt: bytes, source: str = "") -> DmxFrame | None:
    if len(pkt) < 18:
        return None
    sequence = pkt[12]
    universe = pkt[14] | ((pkt[15] & 0x7F) << 8)
    length = struct.unpack_from(">H", pkt, 16)[0]
    data = pkt[18:18 + min(length, 512)]
    return DmxFrame(universe, sequence, data.ljust(512, b"\x00"), source)


def universe_label(port_address: int) -> str:
    return f"{port_address >> 8}:{(port_address >> 4) & 0xF}:{port_address & 0xF} ({port_address})"


def build_poll_reply(ip: str, universe: int, short_name: str, long_name: str,
                     report: str, receiving: bool) -> bytes:
    ip_bytes = socket.inet_aton(ip)
    r = bytearray(239)
    r[0:8] = HEADER
    struct.pack_into("<H", r, 8, OP_POLL_REPLY)
    r[10:14] = ip_bytes
    struct.pack_into("<H", r, 14, ARTNET_PORT)
    struct.pack_into(">H", r, 16, 0x0001)              # firmware version
    r[18] = (universe >> 8) & 0x7F                     # NetSwitch
    r[19] = (universe >> 4) & 0x0F                     # SubSwitch
    struct.pack_into(">H", r, 20, 0x00FF)              # OEM: unknown
    r[23] = 0b11010000                                 # Status1: indicators normal, addresses set by front panel
    r[26:44] = short_name.encode("ascii", "replace")[:17].ljust(18, b"\x00")
    r[44:108] = long_name.encode("ascii", "replace")[:63].ljust(64, b"\x00")
    r[108:172] = report.encode("ascii", "replace")[:63].ljust(64, b"\x00")
    struct.pack_into(">H", r, 172, 1)                  # NumPorts
    r[174] = 0x80                                      # port 0: output from Art-Net, DMX512
    r[182] = 0x80 if receiving else 0x00               # GoodOutputA: data being received
    r[190] = universe & 0x0F                           # SwOut[0]
    r[200] = 0x00                                      # Style: StNode
    r[207:211] = ip_bytes                              # BindIp
    r[211] = 1                                         # BindIndex
    r[212] = 0b00001000                                # Status2: supports 15-bit port-address
    return bytes(r)


def local_ip_for(peer: str) -> str:
    """The local interface address used to reach `peer`."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((peer, ARTNET_PORT))
            return s.getsockname()[0]
    except OSError:
        return "0.0.0.0"


class ArtNetReceiver(asyncio.DatagramProtocol):
    def __init__(self, universe: int, on_frame: Callable[[DmxFrame], None],
                 short_name: str = "Gran2Nan", status: Callable[[], str] = lambda: "OK"):
        self.universe = universe
        self.on_frame = on_frame
        self.short_name = short_name
        self.status = status
        self.transport: asyncio.DatagramTransport | None = None
        self.last_frame_time = 0.0
        self.last_source = ""
        self.frames = 0
        self.last_data = bytes(512)
        self.other_universes: set[int] = set()
        self._poll_count = 0

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data: bytes, addr):
        parsed = parse_packet(data)
        if not parsed:
            return
        op, pkt = parsed
        if op == OP_DMX:
            frame = parse_dmx(pkt, addr[0])
            if not frame:
                return
            if frame.universe != self.universe:
                self.other_universes.add(frame.universe)
                return
            self.frames += 1
            self.last_frame_time = time.monotonic()
            self.last_source = addr[0]
            self.last_data = frame.data
            self.on_frame(frame)
        elif op == OP_POLL:
            self._reply_poll(addr)

    def _reply_poll(self, addr):
        if not self.transport:
            return
        self._poll_count = (self._poll_count + 1) % 10000
        ip = local_ip_for(addr[0])
        receiving = time.monotonic() - self.last_frame_time < 3
        report = f"#0001 [{self._poll_count:04d}] {self.status()}"
        reply = build_poll_reply(ip, self.universe, self.short_name,
                                 f"{self.short_name} (Nanlink WS-TB-1)", report, receiving)
        # Unicast to the poller works with all common consoles and avoids broadcast permission issues
        self.transport.sendto(reply, (addr[0], ARTNET_PORT))


async def start_receiver(bind_ip: str, receiver: ArtNetReceiver) -> asyncio.DatagramTransport:
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind((bind_ip, ARTNET_PORT))
    sock.setblocking(False)
    transport, _ = await loop.create_datagram_endpoint(lambda: receiver, sock=sock)
    return transport
