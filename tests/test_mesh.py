"""Checks against the sample data in the Mesh Profile specification (section 8)."""

from nanlink_dmx import mesh


def h(s):
    return bytes.fromhex(s.replace(" ", ""))


def test_s1():
    assert mesh.s1(b"test") == h("b73cefbd641ef2ea598c2b6efb62f79c")


def test_k2():
    nid, enc, priv = mesh.k2(h("f7a2a44f8e8a8029064f173ddc1e2b00"))
    assert nid == 0x7F
    assert enc == h("9f589181a0f50de73c8070c7a6d27f46")
    assert priv == h("4c715bd4a64b938f99b453351653124f")


def test_k3():
    assert mesh.k3(h("f7a2a44f8e8a8029064f173ddc1e2b00")) == h("ff046958233db014")


def test_k4():
    assert mesh.k4(h("3216d1509884b533248541792b877f98")) == 0x38


def test_sample_message_1_network_pdu():
    # Mesh Profile 8.3.1: control message, Relay -> Friend
    keys = mesh.MeshKeys(h("7dd7364cd842ad18c17c2b820c84c3d6"), bytes(16))
    pdu = mesh.encrypt_network_pdu(keys, 0x12345678, ctl=1, ttl=0, seq=0x000001,
                                   src=0x1201, dst=0xFFFD, transport_pdu=h("034b50057e400000010000"))
    assert pdu == h("68eca487516765b5e5bfdacbaf6cb7fb6bff871f035444ce83a670df")
    dec = mesh.decrypt_network_pdu(keys, 0x12345678, pdu)
    assert dec and dec.src == 0x1201 and dec.dst == 0xFFFD and dec.seq == 1
    assert dec.transport_pdu == h("034b50057e400000010000")


def test_sample_message_6_access():
    # Mesh Profile 8.3.6 uses a device key; check the application path round-trips instead
    keys = mesh.MeshKeys(h("7dd7364cd842ad18c17c2b820c84c3d6"), h("63964771734fbd76e3b40519d1d94a48"))
    access = bytes([0xC1, 0x11, 0x11]) + h("0020010100320001")
    pdu = mesh.build_access_message(keys, 0x12345678, seq=1000, src=0x7F00, dst=0xFFFF, access_pdu=access)
    net = mesh.decrypt_network_pdu(keys, 0x12345678, pdu)
    assert net and net.dst == 0xFFFF and net.src == 0x7F00
    r = mesh.SegmentReassembler().push(net)
    assert r is not None
    upper, seq_auth, szmic = r
    assert mesh.decrypt_access(keys, 0x12345678, net, upper, seq_auth, szmic) == access


def test_segmented_device_key_roundtrip():
    keys = mesh.MeshKeys(h("7dd7364cd842ad18c17c2b820c84c3d6"), h("63964771734fbd76e3b40519d1d94a48"))
    dev_key = bytes(range(16))
    access = b"\x00" + bytes(3) + keys.app_key            # Config AppKey Add, 20 bytes
    counter = iter(range(500, 600))
    pdus = mesh.build_access_segments(keys, 0, lambda: next(counter), 0x7001, 0x0002, access, dev_key)
    assert len(pdus) == 2
    r = mesh.SegmentReassembler()
    out = None
    for p in pdus:
        net = mesh.decrypt_network_pdu(keys, 0, p)
        out = r.push(net) or out
    assert mesh.decrypt_device_access(net, *out, dev_key, 0) == access


def test_parse_key_java_bytes():
    assert mesh.parse_key("[1, -1, 0, 127, -128, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]") == \
        bytes([1, 255, 0, 127, 128, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])


def test_proxy_segmentation_roundtrip():
    payload = bytes(range(60))
    segs = mesh.proxy_segments(mesh.PROXY_NETWORK_PDU, payload, mtu_payload=20)
    assert len(segs) > 1 and all(len(s) <= 20 for s in segs)
    r = mesh.ProxyReassembler()
    out = [r.push(s) for s in segs]
    assert out[-1] == (mesh.PROXY_NETWORK_PDU, payload) and all(o is None for o in out[:-1])
