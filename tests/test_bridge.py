import asyncio
import struct

from nanlink_dmx import artnet, config, engine, fixtures, keyimport, nanlite
from nanlink_dmx.config import Config, FixtureConfig


def artdmx(universe: int, data: bytes, seq: int = 1) -> bytes:
    return (b"Art-Net\x00" + struct.pack("<H", artnet.OP_DMX) + bytes([0, 14, seq, 0])
            + struct.pack("<H", universe) + struct.pack(">H", len(data)) + data)


def test_parse_artdmx():
    op, pkt = artnet.parse_packet(artdmx(0x123, bytes([1, 2, 3])))
    assert op == artnet.OP_DMX
    f = artnet.parse_dmx(pkt)
    assert f.universe == 0x123 and f.data[:4] == bytes([1, 2, 3, 0]) and len(f.data) == 512


def test_poll_reply_layout():
    r = artnet.build_poll_reply("192.168.1.20", 0x0123, "Short", "Long", "#0001 ok", True)
    assert len(r) == 239 and r[:8] == b"Art-Net\x00"
    assert struct.unpack_from("<H", r, 8)[0] == artnet.OP_POLL_REPLY
    assert r[10:14] == bytes([192, 168, 1, 20])
    assert r[18] == 0x01 and r[19] == 0x02 and r[190] == 0x03
    assert r[174] == 0x80 and r[182] == 0x80


def test_personalities():
    p = fixtures.PERSONALITIES["dimmer"]
    assert p.map(bytes([255]), {}) == {"dim": (nanlite.OPT_DIM, 100)}
    assert p.map(bytes([128]), {}) == {"dim": (nanlite.OPT_DIM, 50)}
    cct = fixtures.PERSONALITIES["cct"].map(bytes([0, 255, 128]), {"cct_min": 2700, "cct_max": 6500})
    assert cct["cct"] == (nanlite.OPT_CCT, 6500) and cct["gm"] == (nanlite.OPT_GM, 0)


def test_brightness_command_matches_nanlink_app():
    # captured from the Nanlink app: 50% on channel 1 -> rc 28 01 01 0032 0001
    pdu = nanlite.set_param(nanlite.OPT_DIM, 50, 1)
    assert pdu[:3] == bytes([0xC1, 0x11, 0x11])
    assert pdu[4:] == bytes.fromhex("2801010032 0001".replace(" ", ""))


def test_seqstore_persists_and_never_repeats(tmp_path):
    path = str(tmp_path / "state.json")
    a = config.SeqStore(0, path)
    used = [a.next() for _ in range(5)]
    b = config.SeqStore(0, path)          # restart: same address, higher counter
    src, seq = b.next()
    assert src == used[-1][0] and seq > used[-1][1]
    assert config.AUTO_SRC_RANGE[0] <= src <= config.AUTO_SRC_RANGE[1]


def test_keyimport_parses_feasymesh_dump():
    log = (
        "E MeshNetworkDb: onPostExecute: MeshNetwork{meshUUID='AAAA', ivIndex=IV Index: 7, IV Update Active: false, "
        "netKeys=[NetworkKey{phase=0, key=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, -1], oldKey=null}], "
        "nodes=[ProvisionedMeshNode{mTimeStampInMillis=1, nodeName='My Node', ttl=4, meshUuid='AAAA', "
        "uuid='00112233-4455-0005-0000-00001e014e72', security=0, unicastAddress=5, isConfigured=true}]}\n"
        "V MeshTransport: Key: 00112233445566778899AABBCCDDEEFF\n"
        "V MeshTransport: akf: 1\n"
    )
    r = keyimport.parse_log(log)
    net, node = r.find_box("00:11:22:33:44:55")
    assert net.iv_index == 7 and net.net_key[-1] == 0xFF and node.address == 5
    assert r.app_keys == [bytes.fromhex("00112233445566778899AABBCCDDEEFF")]


class FakeLink:
    state = "connected"
    connected = True

    def __init__(self):
        self.sent = []

    async def send_access(self, pdu, dst=None):
        self.sent.append(pdu)
        return True

    async def stop(self):
        pass


def _bridge():
    cfg = Config()
    cfg.output.max_rate = 100
    cfg.output.settle_repeat = False
    cfg.fixtures = [FixtureConfig("A", 1, 1, "dimmer"), FixtureConfig("B", 2, 2, "dimmer")]
    b = engine.Bridge(cfg)
    b.link = FakeLink()
    return b


def test_engine_sends_only_changes_and_latest_value():
    async def run():
        b = _bridge()
        task = asyncio.create_task(b._sender())
        await asyncio.sleep(0.1)
        b.link.sent.clear()                         # initial full-state send
        b.receiver = artnet.ArtNetReceiver(0, b._on_frame)
        b.receiver.datagram_received(artdmx(0, bytes([255, 0])), ("10.0.0.5", 6454))
        for v in (10, 20, 128):                     # rapid changes coalesce into the latest value
            b.receiver.datagram_received(artdmx(0, bytes([v, 0])), ("10.0.0.5", 6454))
        await asyncio.sleep(0.2)
        task.cancel()
        sent = [(p[6], struct.unpack(">H", p[7:9])[0], struct.unpack(">H", p[9:11])[0]) for p in b.link.sent]
        assert sent == [(nanlite.OPT_DIM, 50, 1)]   # channel 2 never changed, channel 1 only latest
        assert b.control_source == "artnet"

    asyncio.run(run())


def test_manual_control_only_when_console_absent():
    async def run():
        b = _bridge()
        b.receiver = artnet.ArtNetReceiver(0, b._on_frame)
        b.receiver.datagram_received(artdmx(0, bytes([255, 255])), ("10.0.0.5", 6454))
        b.set_manual(0, 0, 0)                       # console live: ignored
        assert b.fixtures[0].dmx[0] == 255
        b.set_manual_only(True)
        b.set_manual(0, 0, 0)
        assert b.fixtures[0].dmx[0] == 0
        b.receiver.datagram_received(artdmx(0, bytes([77, 77])), ("10.0.0.5", 6454))
        assert b.fixtures[0].dmx[0] == 0            # console ignored while manual-only
        b.set_manual_only(False)
        b.receiver.last_frame_time -= 10            # console went away
        b.set_manual(1, 0, 42)
        assert b.fixtures[1].dmx[0] == 42

    asyncio.run(run())
