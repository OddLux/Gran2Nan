"""Pair (provision) a factory-reset WS-TB-1 into our own Bluetooth Mesh network.

Standard Mesh Profile 1.0 provisioning over PB-GATT (no OOB), then
configuration with the box's device key: add our AppKey and bind it to the
Nanlite vendor model. No phone needed.
"""

from __future__ import annotations

import asyncio
import logging
import os
import struct
import sys
from dataclasses import dataclass
from typing import Callable

from bleak import BleakClient, BleakScanner
from cryptography.hazmat.primitives.asymmetric import ec

from . import mesh
from .config import SeqStore
from .link import AUTH_NOTIFY, AUTH_WRITE, MESH_PROXY_SERVICE, PROXY_IN, PROXY_OUT

log = logging.getLogger("nanlink.provision")

PROV_SERVICE = "00001827-0000-1000-8000-00805f9b34fb"
PROV_IN = "00002adb-0000-1000-8000-00805f9b34fb"
PROV_OUT = "00002adc-0000-1000-8000-00805f9b34fb"

INVITE, CAPABILITIES, START, PUBLIC_KEY, INPUT_COMPLETE, CONFIRM, RANDOM, DATA, COMPLETE, FAILED = range(10)

VENDOR_MODEL = (0x1111, 0x1111)   # company, model
# The box's firmware only handles Nanlite commands encrypted with the AppKey the
# Nanlink app uses everywhere (verified: a random AppKey is accepted and bound,
# but vendor commands are then ignored). The NetKey stays our own random key.
NANLINK_APP_KEY = bytes.fromhex("63964771734FBD76E3B40519D1D94A48")


@dataclass
class PairResult:
    address: str
    net_key: bytes
    app_key: bytes
    device_key: bytes
    unicast: int
    iv_index: int = 0


class PairError(RuntimeError):
    pass


async def find_unprovisioned(timeout: float = 15.0, name_hint: str = "WS_TB"):
    """Returns (device, adv) of a nearby device advertising Mesh Provisioning (0x1827)."""
    found = {}

    def cb(dev, adv):
        if PROV_SERVICE in adv.service_uuids or PROV_SERVICE in adv.service_data:
            name = adv.local_name or dev.name or ""
            score = (name_hint.upper() in name.upper(), adv.rssi)
            if "best" not in found or score > found["score"]:
                found.update(best=(dev, adv), score=score)

    async with BleakScanner(cb):
        for _ in range(int(timeout * 10)):
            await asyncio.sleep(0.1)
            if found and found["score"][0]:
                break
    if not found:
        raise PairError("No unpaired box found. Factory-reset the WS-TB-1 and keep it close to the PC.")
    return found["best"]


async def _feasycom_auth(client: BleakClient):
    try:
        ev = asyncio.Event()
        await client.start_notify(AUTH_NOTIFY, lambda *_: ev.set())
        await client.write_gatt_char(AUTH_WRITE, b"$FSCCMD001$\r\n", response=False)
        await asyncio.wait_for(ev.wait(), 2.0)
    except Exception:  # noqa: BLE001 - not every firmware has/needs it
        pass


class _ProvChannel:
    def __init__(self, client: BleakClient):
        self.client = client
        self.rx = mesh.ProxyReassembler()
        self.queue: asyncio.Queue[bytes] = asyncio.Queue()

    async def start(self):
        await self.client.start_notify(PROV_OUT, self._on)

    def _on(self, _, data):
        msg = self.rx.push(bytes(data))
        if msg and msg[0] == mesh.PROXY_PROVISIONING:
            self.queue.put_nowait(msg[1])

    async def send(self, pdu_type: int, params: bytes = b""):
        mtu = max(20, self.client.mtu_size - 3)
        for p in mesh.proxy_segments(mesh.PROXY_PROVISIONING, bytes([pdu_type]) + params, mtu):
            await self.client.write_gatt_char(PROV_IN, p, response=False)

    async def expect(self, pdu_type: int, timeout: float = 15.0) -> bytes:
        try:
            pdu = await asyncio.wait_for(self.queue.get(), timeout)
        except asyncio.TimeoutError:
            raise PairError(f"box did not answer (waiting for provisioning step {pdu_type})") from None
        if pdu[0] == FAILED:
            raise PairError(f"box rejected pairing (error code {pdu[1] if len(pdu) > 1 else '?'})")
        if pdu[0] != pdu_type:
            raise PairError(f"unexpected provisioning PDU {pdu[0]} (expected {pdu_type})")
        return pdu[1:]


async def _provision(client: BleakClient, net_key: bytes, unicast: int, iv_index: int) -> bytes:
    ch = _ProvChannel(client)
    await ch.start()

    invite = bytes([5])                               # attention timer: 5 s
    await ch.send(INVITE, invite)
    caps = await ch.expect(CAPABILITIES)
    if len(caps) < 11:
        raise PairError("invalid capabilities from box")
    start = bytes([0x00, 0x00, 0x00, 0x00, 0x00])     # FIPS P-256, no OOB public key, no OOB auth
    await ch.send(START, start)

    priv = ec.generate_private_key(ec.SECP256R1())
    nums = priv.public_key().public_numbers()
    our_pub = nums.x.to_bytes(32, "big") + nums.y.to_bytes(32, "big")
    await ch.send(PUBLIC_KEY, our_pub)
    dev_pub = await ch.expect(PUBLIC_KEY)
    try:
        peer = ec.EllipticCurvePublicNumbers(int.from_bytes(dev_pub[:32], "big"),
                                             int.from_bytes(dev_pub[32:64], "big"),
                                             ec.SECP256R1()).public_key()
    except ValueError:
        raise PairError("box sent an invalid public key") from None
    secret = priv.exchange(ec.ECDH(), peer)

    conf_inputs = invite + caps[:11] + start + our_pub + dev_pub[:64]
    conf_salt = mesh.s1(conf_inputs)
    conf_key = mesh.k1(secret, conf_salt, b"prck")
    auth = bytes(16)
    our_random = os.urandom(16)
    await ch.send(CONFIRM, mesh.aes_cmac(conf_key, our_random + auth))
    dev_conf = await ch.expect(CONFIRM)
    await ch.send(RANDOM, our_random)
    dev_random = await ch.expect(RANDOM)
    if mesh.aes_cmac(conf_key, dev_random[:16] + auth) != dev_conf[:16]:
        raise PairError("box confirmation mismatch")

    prov_salt = mesh.s1(conf_salt + our_random + dev_random[:16])
    session_key = mesh.k1(secret, prov_salt, b"prsk")
    session_nonce = mesh.k1(secret, prov_salt, b"prsn")[-13:]
    device_key = mesh.k1(secret, prov_salt, b"prdk")
    data = net_key + struct.pack(">HBIH", 0, 0, iv_index, unicast)
    await ch.send(DATA, mesh.ccm_encrypt(session_key, session_nonce, data, 8))
    await ch.expect(COMPLETE, timeout=20)
    return device_key


async def _configure(address: str, keys: mesh.MeshKeys, device_key: bytes, box: int,
                     iv_index: int, seq: SeqStore, progress: Callable[[str], None]):
    """Connect to the freshly provisioned box as a proxy and install our AppKey."""
    device = None
    for _ in range(3):
        device = await BleakScanner.find_device_by_filter(
            lambda d, a: d.address.upper() == address.upper() and
            (MESH_PROXY_SERVICE in a.service_uuids or MESH_PROXY_SERVICE in a.service_data), timeout=10)
        if device:
            break
    if not device:
        raise PairError("box did not come back as a mesh proxy after pairing (try 'Pair box' again)")

    # The box's services change after pairing; don't let Windows reuse its cached list
    extra = {"winrt": {"use_cached_services": False}} if sys.platform == "win32" else {}
    async with BleakClient(device, timeout=20, **extra) as client:
        await _feasycom_auth(client)
        rx, segs = mesh.ProxyReassembler(), mesh.SegmentReassembler()
        replies: asyncio.Queue[bytes] = asyncio.Queue()

        def on_out(_, data):
            msg = rx.push(bytes(data))
            if not msg or msg[0] != mesh.PROXY_NETWORK_PDU:
                return
            net = mesh.decrypt_network_pdu(keys, iv_index, msg[1])
            if not net or net.ctl:
                return
            up = segs.push(net)
            if up:
                acc = mesh.decrypt_device_access(net, *up, device_key, iv_index)
                if acc:
                    replies.put_nowait(acc)

        await client.start_notify(PROXY_OUT, on_out)
        src = seq.next()[0]

        async def write(net_pdus: list[bytes], msg_type=mesh.PROXY_NETWORK_PDU):
            for net in net_pdus:
                for p in mesh.proxy_segments(msg_type, net, max(20, client.mtu_size - 3)):
                    await client.write_gatt_char(PROXY_IN, p, response=False)
                await asyncio.sleep(0.05)

        def next_seq():
            return seq.next()[1]

        await write([mesh.build_proxy_config(keys, iv_index, next_seq(), src,
                                             bytes([mesh.PROXY_CFG_SET_FILTER_TYPE, mesh.FILTER_REJECT_LIST]))],
                    mesh.PROXY_CONFIGURATION)

        async def config_call(access: bytes, status_opcode: bytes, what: str) -> bytes:
            for attempt in range(4):
                await write(mesh.build_access_segments(keys, iv_index, next_seq, src, box, access, device_key))
                try:
                    while True:
                        r = await asyncio.wait_for(replies.get(), 4)
                        if r.startswith(status_opcode):
                            return r[len(status_opcode):]
                except asyncio.TimeoutError:
                    log.info("%s: no reply, retrying", what)
            raise PairError(f"box did not confirm: {what}")

        progress("Installing app key…")
        idx = bytes([0x00, 0x00, 0x00])  # NetKeyIndex 0, AppKeyIndex 0 packed
        st = await config_call(b"\x00" + idx + keys.app_key, b"\x80\x03", "AppKey Add")
        if st[0] not in (0x00, 0x06):  # success / already stored
            raise PairError(f"AppKey Add failed (status {st[0]})")

        progress("Enabling Nanlite light commands…")
        cid, mid = VENDOR_MODEL
        bind = b"\x80\x3d" + struct.pack("<HHHH", box, 0, cid, mid)
        st = await config_call(bind, b"\x80\x3e", "Model App Bind")
        if st[0] != 0x00:
            raise PairError(f"binding the Nanlite model failed (status {st[0]})")


async def pair_box(seq: SeqStore, progress: Callable[[str], None] = lambda s: None,
                   box_unicast: int = 0x0002) -> PairResult:
    progress("Looking for a reset WS-TB-1…")
    dev, adv = await find_unprovisioned()
    progress(f"Found {adv.local_name or dev.name or dev.address} - pairing…")
    net_key, app_key = os.urandom(16), NANLINK_APP_KEY
    async with BleakClient(dev, timeout=20) as client:
        await _feasycom_auth(client)
        device_key = await _provision(client, net_key, box_unicast, 0)
    progress("Paired - waiting for the box to restart as a mesh node…")
    await asyncio.sleep(3)
    keys = mesh.MeshKeys(net_key, app_key)
    await _configure(dev.address, keys, device_key, box_unicast, 0, seq, progress)
    progress("Done.")
    return PairResult(dev.address, net_key, app_key, device_key, box_unicast)
