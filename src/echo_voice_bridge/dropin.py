"""Calls the Echo over the firmware's Drop In protocol (src/hassmic/dropin.c).

Signalling travels as network messages: kind call_id[8] port[2] epk[32] len reason[len]
Audio is UDP: "HMD1" call_id[8] dir seq[4] enc(opus) mac[16], XChaCha20-Poly1305, nonce = call_id dir seq, header as AD.
dir 0 comes from the caller (the bridge), dir 1 from the callee. Opus at 16 kHz mono in 20 ms frames.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import struct
import time
from collections import deque
from collections.abc import Callable

import opuslib
import opuslib.api.ctl as opus_ctl
import opuslib.api.encoder as opus_encoder

from .echonet import EchoNet, Peer, aead_lock, aead_unlock, blake2b, derive, x25519, x25519_keypair

log = logging.getLogger(__name__)

M_INVITE, M_ACCEPT, M_RINGING, M_REFUSE, M_BYE = 1, 2, 3, 4, 5
RATE, FRAME_SAMPLES = 16000, 320
AUDIO_PORT = 28932
INVITE_INTERVAL_S, ANSWER_TIMEOUT_S, RING_TIMEOUT_S = 0.25, 4.0, 30.0
LOST_AFTER_S = 5.0
MAX_CONCEALED_FRAMES = 5


class Call:
    def __init__(self, net: EchoNet, peer: Peer, sock: socket.socket, audio_port: int = AUDIO_PORT):
        self.net, self.peer, self.sock, self.audio_port = net, peer, sock, audio_port
        self.id = os.urandom(8)
        self.secret, self.public = x25519_keypair()
        self.key: bytes | None = None
        self.addr: tuple[str, int] | None = None
        self.seq = 0
        self.rx_high = -1
        self.last_rx = time.monotonic()
        self.rx: deque[bytes] = deque(maxlen=1500)  # 30 s of decoded frames
        # AUDIO rather than VOIP: better quality, and VOIP's high-pass filter would remove the floor tone (audio.py)
        self.encoder = opuslib.Encoder(RATE, 1, opuslib.APPLICATION_AUDIO)
        for ctl, value in ((opus_ctl.set_bitrate, 48000), (opus_ctl.set_complexity, 8)):
            opus_encoder.encoder_ctl(self.encoder.encoder_state, ctl, value)  # some opuslib setters are broken
        self.decoder = opuslib.Decoder(RATE, 1)
        self.stats = {"rx": 0, "tx": 0, "lost": 0, "bad": 0}

    def payload(self, kind: int, reason: bytes = b"") -> bytes:
        return bytes([kind]) + self.id + struct.pack("<H", self.audio_port) + self.public + bytes([len(reason)]) + reason

    def accept(self, port: int, peer_public: bytes) -> None:
        kd = derive(self.net.network_key, b"hassmic drop in 1")
        shared = x25519(self.secret, peer_public)
        if shared == bytes(32):
            raise ValueError("low-order point")
        self.key = blake2b(shared + self.id + self.net.id + self.peer.id, key=kd)
        self.addr = (self.peer.ip, port)
        self.last_rx = time.monotonic()

    def send(self, pcm: bytes) -> None:
        """Encodes and sends one 20 ms frame."""
        if not self.key or not self.addr:
            return
        head = b"HMD1" + self.id + b"\x00" + struct.pack("<I", self.seq)
        enc, mac = aead_lock(self.key, head[4:17] + bytes(11), head, self.encoder.encode(pcm, FRAME_SAMPLES))
        try:
            self.sock.sendto(head + enc + mac, self.addr)
        except OSError as e:
            log.warning("send failed: %s", e)
        self.seq = (self.seq + 1) & 0xffffffff
        self.stats["tx"] += 1

    def on_audio(self, data: bytes, ip: str, port: int) -> None:
        if not self.key or len(data) < 17 + 1 + 16 or data[:4] != b"HMD1" or data[4:12] != self.id or data[12] != 1:
            return
        seq = struct.unpack("<I", data[13:17])[0]
        try:
            opus = aead_unlock(self.key, data[4:17] + bytes(11), data[:17], data[17:-16], data[-16:])
        except Exception:
            self.stats["bad"] += 1
            return
        self.addr = (ip, port)  # as in the firmware: authenticated audio decides the address
        self.last_rx = time.monotonic()
        self.stats["rx"] += 1
        if self.rx_high >= 0:
            gap = (seq - self.rx_high) & 0xffffffff
            if gap == 0 or gap > 0x7fffffff:
                return  # late: playing it would only add latency
            for _ in range(min(gap - 1, MAX_CONCEALED_FRAMES)):
                self.rx.append(self.decoder.decode(b"", FRAME_SAMPLES))
                self.stats["lost"] += 1
        self.rx_high = seq
        self.rx.append(self.decoder.decode(opus, FRAME_SAMPLES))


class DropIn(asyncio.DatagramProtocol):
    def __init__(self, net: EchoNet, on_ended: Callable[[str], None], bind: str = "0.0.0.0", port: int = AUDIO_PORT):
        self.net, self.on_ended, self.bind, self.port = net, on_ended, bind, port
        self.call: Call | None = None
        self.sock: socket.socket | None = None
        self._answer: asyncio.Future | None = None

    async def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.bind, self.port))
        sock.setblocking(False)
        self.sock = sock
        await asyncio.get_running_loop().create_datagram_endpoint(lambda: self, sock=sock)

    def datagram_received(self, data: bytes, addr) -> None:
        if self.call:
            self.call.on_audio(data, addr[0], addr[1])

    async def dial(self, peer: Peer) -> str:
        """Returns "connected" or the reason the call did not connect."""
        if self.call:
            return "already in a call"
        self.call = call = Call(self.net, peer, self.sock, self.port)
        loop = asyncio.get_running_loop()
        self._answer = answer = loop.create_future()
        deadline, rang = time.monotonic() + ANSWER_TIMEOUT_S, False
        while not answer.done() and time.monotonic() < deadline:
            self.net.tell(peer, call.payload(M_INVITE))
            try:
                await asyncio.wait_for(asyncio.shield(answer), INVITE_INTERVAL_S)
            except asyncio.TimeoutError:
                pass
            if answer.done() and answer.result() == "ringing":
                rang = True
                self._answer = answer = loop.create_future()
                deadline = time.monotonic() + RING_TIMEOUT_S
        result = answer.result() if answer.done() else ("not answered" if rang else "no answer")
        if result != "connected":
            self.net.tell(peer, call.payload(M_BYE))
            self.call = None
        return result

    def hangup(self, reason: str = "") -> None:
        call, self.call = self.call, None
        if not call:
            return
        log.info("hanging up (%s): %s", reason or "local", call.stats)
        loop = asyncio.get_running_loop()
        for delay in (0.0, 0.1, 0.25):  # the firmware sends bye three times
            loop.call_later(delay, self.net.tell, call.peer, call.payload(M_BYE, reason.encode()[:60]))

    def on_message(self, peer: Peer, p: bytes) -> None:
        if len(p) < 44 or len(p) < 44 + p[43]:
            return
        kind, call_id, port, public = p[0], p[1:9], struct.unpack("<H", p[9:11])[0], p[11:43]
        reason = p[44:44 + p[43]].decode("utf-8", "replace")
        if kind == M_INVITE:  # the bridge places calls, it does not take them
            why = b"not accepted"
            self.net.tell(peer, bytes([M_REFUSE]) + call_id + struct.pack("<H", self.port) + bytes(32) + bytes([len(why)]) + why)
            return
        call = self.call
        if not call or call_id != call.id:
            return
        pending = self._answer if self._answer and not self._answer.done() else None
        if kind == M_ACCEPT:
            if not call.key:
                call.accept(port, public)
            if pending:
                pending.set_result("connected")
        elif kind == M_RINGING and pending:
            pending.set_result("ringing")
        elif kind == M_REFUSE and pending:
            pending.set_result(reason or "refused")
        elif kind == M_BYE:
            log.info("the Echo hung up: %s; %s", reason or "no reason given", call.stats)
            self.call = None
            self.on_ended(reason or "hung up on the Echo")
