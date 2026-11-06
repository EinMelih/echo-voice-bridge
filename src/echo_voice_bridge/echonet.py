"""Membership in the Echo network of the echo-dot-assist firmware (src/hassmic/arb.c).

The bridge joins as one more member. It hears signed wake word claims and can win a round, which keeps the Echo
from starting its own voice pipeline.

Wire formats (little endian):
  beacon   "HMA1" 1  pub[32] net[8] nlen node [ctr[8] tag[16]]
  claim    "HMA1" 4  net[8] id[8] ctr[8] kw[8] score[4] prio won tag[16]
  message  "HMA1" 7  net[8] from[8] to[8] ctr[8] len payload[len] tag[16]
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import socket
import struct
import time
from collections.abc import Callable
from dataclasses import dataclass

from nacl import bindings as nb

log = logging.getLogger(__name__)

MAGIC = b"HMA1"
T_BEACON, T_CLAIM, T_MSG = 1, 4, 7
ARB_PORT = 28930
BEACON_INTERVAL_S = 25.0
PEER_TTL_S = 300.0
CLAIM_COPY_DELAYS_S = (0.03, 0.08)  # Wi-Fi broadcasts get lost; copies share one counter, as in the firmware


def blake2b(data: bytes, key: bytes = b"", size: int = 32) -> bytes:
    return hashlib.blake2b(data, key=key, digest_size=size).digest()


def x25519_keypair() -> tuple[bytes, bytes]:
    secret = os.urandom(32)
    return secret, nb.crypto_scalarmult_base(secret)


def x25519(secret: bytes, public: bytes) -> bytes:
    return nb.crypto_scalarmult(secret, public)


def aead_lock(key: bytes, nonce: bytes, ad: bytes, plain: bytes) -> tuple[bytes, bytes]:
    """Monocypher's crypto_aead_lock is XChaCha20-Poly1305 (IETF). Returns (ciphertext, mac)."""
    out = nb.crypto_aead_xchacha20poly1305_ietf_encrypt(plain, ad, nonce, key)
    return out[:-16], out[-16:]


def aead_unlock(key: bytes, nonce: bytes, ad: bytes, cipher: bytes, mac: bytes) -> bytes:
    return nb.crypto_aead_xchacha20poly1305_ietf_decrypt(cipher + mac, ad, nonce, key)


def derive(network_key: bytes, label: bytes) -> bytes:
    return blake2b(label, key=network_key)


def wrap_key(secret: bytes, their: bytes, sender: bytes, recipient: bytes, net_id: int) -> bytes:
    shared = x25519(secret, their)
    if shared == bytes(32):
        raise ValueError("low-order point")
    return blake2b(b"hassmic arb key\0" + sender + recipient + struct.pack("<Q", net_id), key=shared)


def seal_network_key(secret: bytes, public: bytes, recipient: bytes, net_id: int, network_key: bytes) -> str:
    """The "key" argument of the firmware's arbitration_key action: pub[32] nonce[24] mac[16] enc(K)[32], base64."""
    nonce = os.urandom(24)
    enc, mac = aead_lock(wrap_key(secret, recipient, public, recipient, net_id), nonce, public, network_key)
    return base64.b64encode(public + nonce + mac + enc).decode()


@dataclass
class Peer:
    public: bytes
    node: str
    ip: str
    counter: int = 0
    seen: float = 0.0
    net: int = 0

    @property
    def id(self) -> bytes:
        return self.public[:8]


class EchoNet(asyncio.DatagramProtocol):
    """on_claim(peer, keyword, score, prio, won) and on_message(peer, payload) run on the event loop."""

    def __init__(self, secret: bytes, public: bytes, network_key: bytes, net_id: int, node: str,
                 on_claim: Callable | None = None, on_message: Callable | None = None,
                 bind: str = "0.0.0.0", port: int = ARB_PORT, broadcast: str = "255.255.255.255"):
        self.secret, self.public, self.network_key, self.net_id, self.node = secret, public, network_key, net_id, node
        self.id = public[:8]
        self.mac_key = derive(network_key, b"hassmic arbitration mac")
        self.on_claim, self.on_message = on_claim, on_message
        self.bind, self.port, self.broadcast = bind, port, broadcast
        self.members: dict[bytes, Peer] = {}
        self.outsiders: dict[bytes, Peer] = {}
        self.transport: asyncio.DatagramTransport | None = None
        self._counter = time.time_ns() // 1000  # keeps rising across restarts; the Echo drops anything lower
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind((self.bind, self.port))
        sock.setblocking(False)
        await asyncio.get_running_loop().create_datagram_endpoint(lambda: self, sock=sock)
        self._task = asyncio.create_task(self._beacons())

    def connection_made(self, transport) -> None:
        self.transport = transport

    def close(self) -> None:
        if self._task:
            self._task.cancel()
        if self.transport:
            self.transport.close()

    async def _beacons(self) -> None:
        while True:
            self.beacon()
            await asyncio.sleep(BEACON_INTERVAL_S)

    # ---------------------------------------------------------------- sending
    def _next_counter(self) -> int:
        self._counter = max(self._counter + 1, time.time_ns() // 1000)
        return self._counter

    def _tag(self, packet: bytes) -> bytes:
        return blake2b(packet, key=self.mac_key, size=16)

    def _send(self, packet: bytes, ip: str | None = None) -> None:
        if self.transport:
            self.transport.sendto(packet, (ip or self.broadcast, self.port))

    def beacon(self, ip: str | None = None) -> None:
        node = self.node.encode()
        p = MAGIC + bytes([T_BEACON]) + self.public + struct.pack("<Q", self.net_id) + bytes([len(node)]) + node
        p += struct.pack("<Q", self._next_counter())
        self._send(p + self._tag(p), ip)

    def claim(self, keyword: bytes, score: int, prio: int, won: bool, ip: str | None = None) -> None:
        p = MAGIC + bytes([T_CLAIM]) + struct.pack("<Q", self.net_id) + self.id + struct.pack("<Q", self._next_counter())
        p += keyword + struct.pack("<i", score) + bytes([prio, 1 if won else 0])
        p += self._tag(p)
        # The first copy goes out now. A later message with a higher counter must not overtake it: the Echo would
        # reject the claim as a replay and answer the wake word itself.
        self._send(p, ip)
        loop = asyncio.get_running_loop()
        for delay in CLAIM_COPY_DELAYS_S:
            loop.call_later(delay, self._send, p, ip)

    def tell(self, peer: Peer, payload: bytes) -> None:
        p = MAGIC + bytes([T_MSG]) + struct.pack("<Q", self.net_id) + self.id + peer.id
        p += struct.pack("<Q", self._next_counter()) + bytes([len(payload)]) + payload
        self._send(p + self._tag(p), peer.ip)

    # ---------------------------------------------------------------- receiving
    def member(self, node: str) -> Peer | None:
        now = time.monotonic()
        for peer in self.members.values():
            if peer.node == node and now - peer.seen < PEER_TTL_S:
                return peer
        return None

    def outsider(self, node: str) -> Peer | None:
        return next((p for p in self.outsiders.values() if p.node == node), None)

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            self._handle(data, addr[0])
        except Exception:
            log.exception("bad packet from %s", addr[0])

    def _authentic(self, data: bytes) -> bool:
        return len(data) > 16 and self._tag(data[:-16]) == data[-16:]

    def _fresh(self, peer_id: bytes, counter: int) -> Peer | None:
        peer = self.members.get(peer_id)
        if peer is None or counter <= peer.counter:
            return None
        peer.counter, peer.seen = counter, time.monotonic()
        return peer

    def _handle(self, data: bytes, ip: str) -> None:
        if data[:4] != MAGIC or len(data) < 5:
            return
        kind, r = data[4], data[5:]
        if kind == T_BEACON:
            self._on_beacon(data, r, ip)
        elif kind == T_CLAIM and len(r) == 8 + 8 + 8 + 8 + 4 + 2 + 16:
            net, peer_id, counter = struct.unpack("<Q", r[:8])[0], r[8:16], struct.unpack("<Q", r[16:24])[0]
            if net != self.net_id or peer_id == self.id or not self._authentic(data):
                return
            peer = self._fresh(peer_id, counter)
            if peer and self.on_claim:
                self.on_claim(peer, r[24:32], struct.unpack("<i", r[32:36])[0], r[36], bool(r[37]))
        elif kind == T_MSG and len(r) >= 8 + 8 + 8 + 8 + 1 + 16:
            net, peer_id, to, counter = struct.unpack("<Q", r[:8])[0], r[8:16], r[16:24], struct.unpack("<Q", r[24:32])[0]
            size = r[32]
            if net != self.net_id or to != self.id or len(r) != 33 + size + 16 or not self._authentic(data):
                return
            peer = self._fresh(peer_id, counter)
            if peer:
                peer.ip = ip
                if self.on_message:
                    self.on_message(peer, r[33:33 + size])

    def _on_beacon(self, data: bytes, r: bytes, ip: str) -> None:
        if len(r) < 41:
            return
        public, net, size = r[:32], struct.unpack("<Q", r[32:40])[0], r[40]
        node = r[41:41 + size].decode("utf-8", "replace")
        if public == self.public:
            return
        if net != self.net_id:
            self.outsiders[public[:8]] = Peer(public, node, ip, seen=time.monotonic(), net=net)
            return
        if not self._authentic(data):
            log.warning("beacon from %s failed authentication", node)
            return
        counter = struct.unpack("<Q", r[41 + size:49 + size])[0]
        peer = self.members.get(public[:8])
        known = peer is not None
        if peer is None:
            peer = self.members[public[:8]] = Peer(public, node, ip)
            log.info("member %s (%s) joined the network", node, ip)
        peer.node, peer.ip = node, ip
        if counter > peer.counter:
            peer.counter, peer.seen = counter, time.monotonic()
            if not known:
                self.beacon()  # it only counts us after hearing our beacon
