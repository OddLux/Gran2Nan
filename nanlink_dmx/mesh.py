"""Bluetooth Mesh crypto and PDU handling (Mesh Profile 1.0, proxy bearer).

Only what a GATT proxy client needs: key derivation, network/application layer
encryption of unsegmented access messages, proxy configuration messages,
secure network beacon parsing, and decryption of incoming messages
(including reassembly of segmented ones).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Callable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESCCM
from cryptography.hazmat.primitives.cmac import CMAC
from cryptography.exceptions import InvalidTag

# Proxy PDU message types
PROXY_NETWORK_PDU = 0x00
PROXY_MESH_BEACON = 0x01
PROXY_CONFIGURATION = 0x02
PROXY_PROVISIONING = 0x03

# Proxy PDU SAR field
SAR_COMPLETE = 0b00
SAR_FIRST = 0b01
SAR_CONTINUATION = 0b10
SAR_LAST = 0b11

ADDR_ALL_NODES = 0xFFFF

# Proxy configuration opcodes
PROXY_CFG_SET_FILTER_TYPE = 0x00
PROXY_CFG_ADD_ADDRESSES = 0x01
PROXY_CFG_FILTER_STATUS = 0x03
FILTER_ACCEPT_LIST = 0x00
FILTER_REJECT_LIST = 0x01

SEQ_MAX = 0xFFFFFF


# --------------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------------

def aes_cmac(key: bytes, msg: bytes) -> bytes:
    c = CMAC(algorithms.AES(key))
    c.update(msg)
    return c.finalize()


def aes_ecb(key: bytes, block: bytes) -> bytes:
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return enc.update(block) + enc.finalize()


def ccm_encrypt(key: bytes, nonce: bytes, plaintext: bytes, mic_len: int) -> bytes:
    return AESCCM(key, tag_length=mic_len).encrypt(nonce, plaintext, None)


def ccm_decrypt(key: bytes, nonce: bytes, ciphertext: bytes, mic_len: int) -> bytes | None:
    try:
        return AESCCM(key, tag_length=mic_len).decrypt(nonce, ciphertext, None)
    except InvalidTag:
        return None


def s1(m: bytes) -> bytes:
    return aes_cmac(bytes(16), m)


def k1(n: bytes, salt: bytes, p: bytes) -> bytes:
    return aes_cmac(aes_cmac(salt, n), p)


def k2(n: bytes, p: bytes = b"\x00") -> tuple[int, bytes, bytes]:
    t = aes_cmac(s1(b"smk2"), n)
    t1 = aes_cmac(t, p + b"\x01")
    t2 = aes_cmac(t, t1 + p + b"\x02")
    t3 = aes_cmac(t, t2 + p + b"\x03")
    return t1[15] & 0x7F, t2, t3


def k3(n: bytes) -> bytes:
    t = aes_cmac(s1(b"smk3"), n)
    return aes_cmac(t, b"id64\x01")[8:]


def k4(n: bytes) -> int:
    t = aes_cmac(s1(b"smk4"), n)
    return aes_cmac(t, b"id6\x01")[15] & 0x3F


# --------------------------------------------------------------------------
# Keys
# --------------------------------------------------------------------------

@dataclass
class MeshKeys:
    net_key: bytes
    app_key: bytes
    nid: int = field(init=False)
    encryption_key: bytes = field(init=False)
    privacy_key: bytes = field(init=False)
    network_id: bytes = field(init=False)
    beacon_key: bytes = field(init=False)
    aid: int = field(init=False)

    def __post_init__(self):
        if len(self.net_key) != 16 or len(self.app_key) != 16:
            raise ValueError("NetKey and AppKey must be 16 bytes each")
        self.nid, self.encryption_key, self.privacy_key = k2(self.net_key)
        self.network_id = k3(self.net_key)
        self.beacon_key = k1(self.net_key, s1(b"nkbk"), b"id128\x01")
        self.aid = k4(self.app_key)


def parse_key(text: str) -> bytes:
    """Accept '0011..ff' hex (with optional spaces/colons) or a Java-style
    signed byte list '[107, 91, -113, ...]' as printed in Android logs."""
    text = text.strip()
    if text.startswith("["):
        vals = [int(v) & 0xFF for v in text.strip("[]").split(",") if v.strip()]
        key = bytes(vals)
    else:
        key = bytes.fromhex(text.replace(" ", "").replace(":", "").replace("-", ""))
    if len(key) != 16:
        raise ValueError(f"key must be 16 bytes, got {len(key)}")
    return key


# --------------------------------------------------------------------------
# Nonces
# --------------------------------------------------------------------------

def _seq3(seq: int) -> bytes:
    return struct.pack(">I", seq)[1:]


def network_nonce(ctl_ttl: int, seq: int, src: int, iv_index: int) -> bytes:
    return bytes([0x00, ctl_ttl]) + _seq3(seq) + struct.pack(">HHI", src, 0, iv_index)


def application_nonce(seq: int, src: int, dst: int, iv_index: int, szmic: int = 0) -> bytes:
    return bytes([0x01, (szmic & 1) << 7]) + _seq3(seq) + struct.pack(">HHI", src, dst, iv_index)


def proxy_nonce(seq: int, src: int, iv_index: int) -> bytes:
    return bytes([0x03, 0x00]) + _seq3(seq) + struct.pack(">HHI", src, 0, iv_index)


# --------------------------------------------------------------------------
# Network layer
# --------------------------------------------------------------------------

def _pecb(keys: MeshKeys, iv_index: int, privacy_random: bytes) -> bytes:
    return aes_ecb(keys.privacy_key, bytes(5) + struct.pack(">I", iv_index) + privacy_random[:7])


def encrypt_network_pdu(keys: MeshKeys, iv_index: int, ctl: int, ttl: int, seq: int,
                        src: int, dst: int, transport_pdu: bytes, proxy: bool = False) -> bytes:
    ctl_ttl = ((ctl & 1) << 7) | (ttl & 0x7F)
    mic_len = 8 if ctl else 4
    nonce = proxy_nonce(seq, src, iv_index) if proxy else network_nonce(ctl_ttl, seq, src, iv_index)
    enc = ccm_encrypt(keys.encryption_key, nonce, struct.pack(">H", dst) + transport_pdu, mic_len)
    pecb = _pecb(keys, iv_index, enc)
    hdr = bytes([ctl_ttl]) + _seq3(seq) + struct.pack(">H", src)
    obf = bytes(a ^ b for a, b in zip(hdr, pecb))
    return bytes([((iv_index & 1) << 7) | keys.nid]) + obf + enc


@dataclass
class NetworkPdu:
    ctl: int
    ttl: int
    seq: int
    src: int
    dst: int
    transport_pdu: bytes


def decrypt_network_pdu(keys: MeshKeys, iv_index: int, pdu: bytes,
                        proxy: bool = False) -> NetworkPdu | None:
    if len(pdu) < 14 or (pdu[0] & 0x7F) != keys.nid:
        return None
    # The IVI bit tells us whether the sender used the current or previous IV index
    if (pdu[0] >> 7) != (iv_index & 1):
        iv_index -= 1
    enc = pdu[7:]
    pecb = _pecb(keys, iv_index, enc)
    hdr = bytes(a ^ b for a, b in zip(pdu[1:7], pecb))
    ctl_ttl = hdr[0]
    ctl = ctl_ttl >> 7
    seq = int.from_bytes(hdr[1:4], "big")
    src = int.from_bytes(hdr[4:6], "big")
    nonce = proxy_nonce(seq, src, iv_index) if proxy else network_nonce(ctl_ttl, seq, src, iv_index)
    plain = ccm_decrypt(keys.encryption_key, nonce, enc, 8 if ctl else 4)
    if plain is None:
        return None
    return NetworkPdu(ctl, ctl_ttl & 0x7F, seq, src, int.from_bytes(plain[:2], "big"), plain[2:])


# --------------------------------------------------------------------------
# Transport / access layer
# --------------------------------------------------------------------------

def build_access_message(keys: MeshKeys, iv_index: int, seq: int, src: int, dst: int,
                         access_pdu: bytes, ttl: int = 5) -> bytes:
    """Unsegmented application-key access message -> network PDU.

    Unsegmented messages carry at most 11 bytes of access payload
    (15 bytes of upper transport PDU including the 4-byte TransMIC)."""
    if len(access_pdu) > 11:
        raise ValueError("access PDU too long for an unsegmented message")
    upper = ccm_encrypt(keys.app_key, application_nonce(seq, src, dst, iv_index), access_pdu, 4)
    lower = bytes([0x40 | keys.aid]) + upper  # SEG=0, AKF=1
    return encrypt_network_pdu(keys, iv_index, 0, ttl, seq, src, dst, lower)


def device_nonce(seq: int, src: int, dst: int, iv_index: int, szmic: int = 0) -> bytes:
    return bytes([0x02, (szmic & 1) << 7]) + _seq3(seq) + struct.pack(">HHI", src, dst, iv_index)


def build_access_segments(keys: MeshKeys, iv_index: int, seqs: "Callable[[], int]", src: int, dst: int,
                          access_pdu: bytes, device_key: bytes | None = None, ttl: int = 5) -> list[bytes]:
    """Access message of any length -> list of network PDUs (segmented when needed).

    With device_key the message is encrypted for configuration (AKF=0)."""
    seq = seqs()
    if device_key:
        upper = ccm_encrypt(device_key, device_nonce(seq, src, dst, iv_index), access_pdu, 4)
        akf_aid = 0x00
    else:
        upper = ccm_encrypt(keys.app_key, application_nonce(seq, src, dst, iv_index), access_pdu, 4)
        akf_aid = 0x40 | keys.aid
    if len(upper) <= 15:
        return [encrypt_network_pdu(keys, iv_index, 0, ttl, seq, src, dst, bytes([akf_aid]) + upper)]
    chunks = [upper[i:i + 12] for i in range(0, len(upper), 12)]
    seg_n, seq_zero = len(chunks) - 1, seq & 0x1FFF
    out = []
    for seg_o, chunk in enumerate(chunks):
        s = seq if seg_o == 0 else seqs()
        hdr = struct.pack(">I", (seq_zero << 10) | (seg_o << 5) | seg_n)[1:]
        out.append(encrypt_network_pdu(keys, iv_index, 0, ttl, s, src, dst, bytes([0x80 | akf_aid]) + hdr + chunk))
    return out


def decrypt_device_access(net: NetworkPdu, upper: bytes, seq_auth: int, szmic: int,
                          device_key: bytes, iv_index: int) -> bytes | None:
    if (net.transport_pdu[0] >> 6) & 1:
        return None
    nonce = device_nonce(seq_auth, net.src, net.dst, iv_index, szmic)
    return ccm_decrypt(device_key, nonce, upper, 8 if szmic else 4)


def build_proxy_config(keys: MeshKeys, iv_index: int, seq: int, src: int, payload: bytes) -> bytes:
    return encrypt_network_pdu(keys, iv_index, 1, 0, seq, src, 0x0000, payload, proxy=True)


class SegmentReassembler:
    """Collects segments of incoming segmented access messages."""

    def __init__(self):
        self._pending: dict[tuple[int, int], dict] = {}

    def push(self, net: NetworkPdu) -> tuple[bytes, int, int] | None:
        """Returns (upper transport PDU, seq_auth, szmic) once complete."""
        lt = net.transport_pdu
        if not lt or net.ctl:
            return None
        if not lt[0] & 0x80:
            return lt[1:], net.seq, 0
        if len(lt) < 5:
            return None
        hdr = int.from_bytes(lt[1:4], "big")
        szmic = hdr >> 23
        seq_zero = (hdr >> 10) & 0x1FFF
        seg_o = (hdr >> 5) & 0x1F
        seg_n = hdr & 0x1F
        key = (net.src, seq_zero)
        entry = self._pending.setdefault(key, {"segs": {}, "seg_n": seg_n})
        entry["segs"][seg_o] = lt[4:]
        if len(entry["segs"]) != seg_n + 1:
            return None
        del self._pending[key]
        upper = b"".join(entry["segs"][i] for i in range(seg_n + 1))
        # SeqAuth: the low 13 bits come from SeqZero, upper bits from this segment's SEQ
        seq_auth = (net.seq & ~0x1FFF) | seq_zero
        if seq_auth > net.seq:
            seq_auth -= 0x2000
        return upper, seq_auth, szmic


def decrypt_access(keys: MeshKeys, iv_index: int, net: NetworkPdu, upper: bytes,
                   seq_auth: int, szmic: int) -> bytes | None:
    akf = (net.transport_pdu[0] >> 6) & 1
    if not akf:
        return None  # device-key messages are not for us
    nonce = application_nonce(seq_auth, net.src, net.dst, iv_index, szmic)
    return ccm_decrypt(keys.app_key, nonce, upper, 8 if szmic else 4)


# --------------------------------------------------------------------------
# Beacons
# --------------------------------------------------------------------------

@dataclass
class SecureBeacon:
    key_refresh: bool
    iv_update: bool
    network_id: bytes
    iv_index: int
    authentic: bool


def parse_secure_beacon(keys: MeshKeys, data: bytes) -> SecureBeacon | None:
    if len(data) < 22 or data[0] != 0x01:
        return None
    flags = data[1]
    network_id = data[2:10]
    iv_index = int.from_bytes(data[10:14], "big")
    auth = aes_cmac(keys.beacon_key, data[1:14])[:8]
    return SecureBeacon(bool(flags & 1), bool(flags & 2), network_id, iv_index,
                        auth == data[14:22] and network_id == keys.network_id)


# --------------------------------------------------------------------------
# Proxy bearer framing
# --------------------------------------------------------------------------

def proxy_segments(msg_type: int, payload: bytes, mtu_payload: int) -> list[bytes]:
    """Split a proxy message into ATT-sized proxy PDUs."""
    room = mtu_payload - 1
    if len(payload) <= room:
        return [bytes([(SAR_COMPLETE << 6) | msg_type]) + payload]
    chunks = [payload[i:i + room] for i in range(0, len(payload), room)]
    out = []
    for i, chunk in enumerate(chunks):
        sar = SAR_FIRST if i == 0 else SAR_LAST if i == len(chunks) - 1 else SAR_CONTINUATION
        out.append(bytes([(sar << 6) | msg_type]) + chunk)
    return out


class ProxyReassembler:
    def __init__(self):
        self._buf = b""
        self._type: int | None = None

    def push(self, pdu: bytes) -> tuple[int, bytes] | None:
        if not pdu:
            return None
        sar, msg_type, data = pdu[0] >> 6, pdu[0] & 0x3F, pdu[1:]
        if sar == SAR_COMPLETE:
            self._type = None
            return msg_type, data
        if sar == SAR_FIRST:
            self._type, self._buf = msg_type, data
            return None
        if self._type != msg_type:
            self._type = None
            return None
        self._buf += data
        if sar == SAR_LAST:
            self._type = None
            return msg_type, self._buf
        return None
