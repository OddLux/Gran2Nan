"""BLE connection to the WS-TB-1 acting as a Bluetooth Mesh GATT proxy."""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from bleak import BleakClient, BleakScanner

from . import mesh
from .config import MeshConfig, SeqStore

log = logging.getLogger("nanlink.link")

PROXY_IN = "00002add-0000-1000-8000-00805f9b34fb"
PROXY_OUT = "00002ade-0000-1000-8000-00805f9b34fb"
AUTH_WRITE = "0000fff2-0000-1000-8000-00805f9b34fb"
AUTH_NOTIFY = "0000fff1-0000-1000-8000-00805f9b34fb"
MESH_PROXY_SERVICE = "00001828-0000-1000-8000-00805f9b34fb"

DISCONNECTED, SCANNING, CONNECTING, CONNECTED = "disconnected", "scanning", "connecting", "connected"


class MeshLink:
    """Keeps a connection to the box and sends encrypted mesh messages through it."""

    def __init__(self, mesh_cfg: MeshConfig, device_address: str, device_name: str,
                 seq: SeqStore, on_state: Callable[[str, str], None] = lambda s, d: None,
                 on_access: Callable[[int, bytes], None] = lambda src, pdu: None):
        self.keys = mesh.MeshKeys(mesh.parse_key(mesh_cfg.net_key), mesh.parse_key(mesh_cfg.app_key))
        self.cfg = mesh_cfg
        self.iv_index = mesh_cfg.iv_index
        self.device_address = device_address.strip()
        self.device_name = device_name.strip()
        self.seq = seq
        self.on_state = on_state
        self.on_access = on_access
        self.state = DISCONNECTED
        self.sent = 0
        self._client: BleakClient | None = None
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._connected = asyncio.Event()
        self._proxy_rx = mesh.ProxyReassembler()
        self._segments = mesh.SegmentReassembler()
        self._write_lock = asyncio.Lock()

    # ---- lifecycle -------------------------------------------------------

    def start(self):
        self._stop.clear()
        self._task = asyncio.create_task(self._run())

    async def stop(self):
        self._stop.set()
        if self._client and self._client.is_connected:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._set_state(DISCONNECTED, "stopped")

    @property
    def connected(self) -> bool:
        return self.state == CONNECTED

    def _set_state(self, state: str, detail: str = ""):
        self.state = state
        if state == CONNECTED:
            self._connected.set()
        else:
            self._connected.clear()
        self.on_state(state, detail)

    async def _run(self):
        delay = 1.0
        while not self._stop.is_set():
            try:
                await self._session()
                delay = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - any BLE failure means reconnect
                log.warning("BLE: %s", e)
                self._set_state(DISCONNECTED, str(e) or type(e).__name__)
            if self._stop.is_set():
                break
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 10.0)

    async def _find_device(self):
        self._set_state(SCANNING, self.device_address or self.device_name)
        by_addr, by_name = {}, {}

        def cb(dev, adv):
            if self.device_address and dev.address.upper() == self.device_address.upper():
                by_addr.setdefault("hit", (dev, adv))
            elif self.device_name and (adv.local_name or dev.name or "") == self.device_name:
                by_name.setdefault("hit", (dev, adv))

        # The configured address wins; otherwise fall back to the advertised name
        # (macOS reports random device IDs instead of MAC addresses)
        async with BleakScanner(cb):
            for _ in range(100):
                if by_addr or (by_name and not self.device_address) or self._stop.is_set():
                    break
                await asyncio.sleep(0.1)
        hit = by_addr.get("hit") or by_name.get("hit")
        if not hit:
            raise RuntimeError("WS-TB-1 not found (is it powered on and in range?)")
        return hit

    def _check_network(self, adv):
        sdata = adv.service_data.get(MESH_PROXY_SERVICE)
        if sdata and sdata[0] == 0x00 and len(sdata) >= 9:
            if sdata[1:9] != self.keys.network_id:
                raise RuntimeError(
                    "the box belongs to a different mesh network than the configured keys "
                    "(re-import keys from the Nanlink app)")
            return True
        return False

    async def _session(self):
        dev, adv = await self._find_device()
        self._check_network(adv)
        self._set_state(CONNECTING, dev.address)
        disconnected = asyncio.Event()
        client = BleakClient(dev, disconnected_callback=lambda _: disconnected.set(), timeout=20.0)
        self._client = client
        await client.connect()
        try:
            auth = asyncio.Event()
            await client.start_notify(AUTH_NOTIFY, lambda _, d: auth.set())
            await client.write_gatt_char(AUTH_WRITE, b"$FSCCMD001$\r\n", response=False)
            try:
                await asyncio.wait_for(auth.wait(), 2.0)
            except asyncio.TimeoutError:
                log.info("no auth reply from box, continuing")
            await client.start_notify(PROXY_OUT, self._on_proxy_out)
            # Ask the proxy to forward everything (reject list with no entries)
            await self._send_raw(mesh.PROXY_CONFIGURATION, self._proxy_config(
                bytes([mesh.PROXY_CFG_SET_FILTER_TYPE, mesh.FILTER_REJECT_LIST])))
            self._set_state(CONNECTED, dev.address)
            stop_wait = asyncio.create_task(self._stop.wait())
            disc_wait = asyncio.create_task(disconnected.wait())
            await asyncio.wait({stop_wait, disc_wait}, return_when=asyncio.FIRST_COMPLETED)
            stop_wait.cancel()
            disc_wait.cancel()
            if not self._stop.is_set():
                raise RuntimeError("connection lost")
        finally:
            self._client = None
            if client.is_connected:
                try:
                    await client.disconnect()
                except Exception:
                    pass

    # ---- sending ---------------------------------------------------------

    def _proxy_config(self, payload: bytes) -> bytes:
        src, seq = self.seq.next()
        return mesh.build_proxy_config(self.keys, self._tx_iv(), seq, src, payload)

    def _tx_iv(self) -> int:
        return self.iv_index

    async def _send_raw(self, msg_type: int, payload: bytes):
        client = self._client
        if not client:
            raise RuntimeError("not connected")
        async with self._write_lock:
            for pdu in mesh.proxy_segments(msg_type, payload, max(20, client.mtu_size - 3)):
                await client.write_gatt_char(PROXY_IN, pdu, response=False)

    async def send_access(self, access_pdu: bytes, dst: int | None = None) -> bool:
        """Send one unsegmented access message. Returns False when not connected."""
        if not self.connected:
            return False
        src = self.seq.next()[0]
        nets = mesh.build_access_segments(self.keys, self._tx_iv(), lambda: self.seq.next()[1], src,
                                          self.cfg.box_address if dst is None else dst,
                                          access_pdu, ttl=self.cfg.ttl)
        try:
            for net in nets:
                await self._send_raw(mesh.PROXY_NETWORK_PDU, net)
        except Exception as e:  # noqa: BLE001
            log.warning("send failed: %s", e)
            return False
        self.sent += 1
        return True

    async def wait_connected(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._connected.wait(), timeout)
            return True
        except asyncio.TimeoutError:
            return False

    # ---- receiving -------------------------------------------------------

    def _on_proxy_out(self, _, data: bytearray):
        msg = self._proxy_rx.push(bytes(data))
        if not msg:
            return
        msg_type, payload = msg
        if msg_type == mesh.PROXY_MESH_BEACON:
            beacon = mesh.parse_secure_beacon(self.keys, payload)
            if beacon and beacon.authentic:
                iv = beacon.iv_index - 1 if beacon.iv_update else beacon.iv_index
                if iv != self.iv_index:
                    log.info("IV index from beacon: %d", iv)
                    self.iv_index = iv
        elif msg_type == mesh.PROXY_NETWORK_PDU:
            net = mesh.decrypt_network_pdu(self.keys, self.iv_index, payload)
            if not net:
                return
            upper = self._segments.push(net)
            if upper:
                access = mesh.decrypt_access(self.keys, self.iv_index, net, *upper)
                if access:
                    self.on_access(net.src, access)


async def scan_for_boxes(timeout: float = 6.0) -> list[tuple[str, str, int]]:
    """Returns [(address, name, rssi)] for nearby mesh proxies / WS-TB devices."""
    found = await BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for addr, (dev, adv) in found.items():
        name = adv.local_name or dev.name or ""
        if MESH_PROXY_SERVICE in adv.service_uuids or MESH_PROXY_SERVICE in adv.service_data \
                or "WS_TB" in name.upper():
            out.append((addr, name, adv.rssi))
    return sorted(out, key=lambda t: -t[2])
