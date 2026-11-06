"""A simulated Echo that speaks the firmware's real packets over loopback: the bridge on 127.0.0.1, the Echo on 127.0.0.2."""
from __future__ import annotations

import asyncio
import os
import struct

import opuslib
import pytest

from echo_voice_bridge.backends.base import VoiceBackend
from echo_voice_bridge.config import Config, load_identity
from echo_voice_bridge.dropin import M_ACCEPT, M_BYE, M_INVITE, M_REFUSE
from echo_voice_bridge.echonet import EchoNet, aead_lock, aead_unlock, blake2b, derive, x25519, x25519_keypair

KEYWORD = blake2b(b"alexa", size=8)
AUDIO_PORT = 38932
NET_PORT = 38930


class FakeEcho(asyncio.DatagramProtocol):
    def __init__(self, identity: dict, refuse: str = ""):
        secret, public = x25519_keypair()
        self.refuse = refuse
        self.net = EchoNet(secret, public, identity["network_key"], identity["net_id"], "echo-test",
                           on_claim=self.on_claim, on_message=self.on_message,
                           bind="127.0.0.2", port=NET_PORT, broadcast="127.0.0.1")
        self.events: list[str] = []
        self.won = asyncio.Event()
        self.key = self.call_id = self.bridge = self.addr = None
        self.decoder = opuslib.Decoder(16000, 1)
        self.encoder = opuslib.Encoder(16000, 1, opuslib.APPLICATION_VOIP)
        self.received: list[bytes] = []
        self.seq = 0
        self.transport = None

    async def start(self) -> None:
        await self.net.start()
        await asyncio.get_running_loop().create_datagram_endpoint(lambda: self, local_addr=("127.0.0.2", AUDIO_PORT))

    def connection_made(self, transport) -> None:
        self.transport = transport

    def on_claim(self, peer, keyword, score, prio, won) -> None:
        self.events.append("claim-won" if won else "claim")
        if won:
            self.won.set()

    def on_message(self, peer, p) -> None:
        kind, call_id, port, public = p[0], p[1:9], struct.unpack("<H", p[9:11])[0], p[11:43]
        self.events.append({M_INVITE: "invite", M_BYE: "bye"}.get(kind, str(kind)))
        if kind == M_INVITE and self.key is None:
            if self.refuse:
                why = self.refuse.encode()
                self.net.tell(peer, bytes([M_REFUSE]) + call_id + struct.pack("<H", AUDIO_PORT) + bytes(32) + bytes([len(why)]) + why)
                return
            secret, mine = x25519_keypair()
            kd = derive(self.net.network_key, b"hassmic drop in 1")
            self.key = blake2b(x25519(secret, public) + call_id + peer.id + self.net.id, key=kd)
            self.call_id, self.bridge, self.addr = call_id, peer, (peer.ip, port)
            self.net.tell(peer, bytes([M_ACCEPT]) + call_id + struct.pack("<H", AUDIO_PORT) + mine + bytes([0]))

    def datagram_received(self, data: bytes, addr) -> None:  # audio from the bridge
        if self.key and data[:4] == b"HMD1" and data[12] == 0:
            opus = aead_unlock(self.key, data[4:17] + bytes(11), data[:17], data[17:-16], data[-16:])
            self.received.append(self.decoder.decode(opus, 320))

    def send_mic(self, pcm: bytes) -> None:
        head = b"HMD1" + self.call_id + b"\x01" + struct.pack("<I", self.seq)
        enc, mac = aead_lock(self.key, head[4:17] + bytes(11), head, self.encoder.encode(pcm, 320))
        self.transport.sendto(head + enc + mac, self.addr)
        self.seq += 1

    def wake(self, prio: int = 0) -> None:
        self.net.claim(KEYWORD, 1500, prio, False, "127.0.0.1")

    def hang_up(self) -> None:
        self.net.tell(self.bridge, bytes([M_BYE]) + self.call_id + struct.pack("<H", AUDIO_PORT) + bytes(32) + bytes([4]) + b"stop")

    def close(self) -> None:
        self.net.close()
        if self.transport:
            self.transport.close()


class FakeBackend(VoiceBackend):
    def __init__(self):
        self.started = self.stopped = 0
        self.mic: list[bytes] = []

    async def start(self) -> None:
        self.started += 1

    async def stop(self) -> None:
        self.stopped += 1

    def send_audio(self, pcm: bytes) -> None:
        self.mic.append(pcm)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Runs a test coroutine with a started bridge and simulated Echo that already know each other."""
    from echo_voice_bridge.bridge import Bridge

    def run(test, refuse: str = "", **config):
        async def main():
            cfg = Config(echo_node="echo-test", state_dir=tmp_path, status_port=0, openai_key="x", ha_url="", ha_token="",
                         ha_media_player="", ha_light="", transcript_dir=str(tmp_path / "log"), **config)
            backend = FakeBackend()
            bridge = Bridge(cfg, backend, {"net": {"bind": "127.0.0.1", "port": NET_PORT, "broadcast": "127.0.0.2"},
                                           "dropin": {"bind": "127.0.0.1", "port": AUDIO_PORT}})
            echo = FakeEcho(load_identity(tmp_path), refuse)
            await bridge.net.start()
            await bridge.dropin.start()
            await echo.start()
            bridge.net.beacon()
            echo.net.beacon()
            for _ in range(50):
                if bridge.net.member("echo-test") and echo.net.member(cfg.node):
                    break
                await asyncio.sleep(0.02)
            try:
                await asyncio.wait_for(test(bridge, echo, backend), 20)
            finally:
                if bridge.session and not bridge.session.done():
                    bridge.on_call_ended("test over")
                    await asyncio.wait({bridge.session}, timeout=5)
                echo.close()
                bridge.net.close()
        asyncio.run(main())
    os.environ.pop("EVB_ECHO_NODE", None)
    return run
