"""Wake word to call to conversation, and back to standby."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp

from .audio import AudioPump, Playback, ReplyProcessor, floor_tone
from .backends.base import VoiceBackend
from .config import Config, load_identity
from .dropin import LOST_AFTER_S, DropIn
from .echonet import EchoNet, Peer, seal_network_key
from .transcript import Transcript

log = logging.getLogger(__name__)

WIN_SCORE, LOSE_SCORE = 32767, -32768
REJOIN_S = 300
RECONNECT_DELAYS_S = (1, 3, 10)
USER_RMS = 120


class Bridge:
    def __init__(self, cfg: Config, backend: VoiceBackend, net_options: dict | None = None):
        self.cfg, self.backend = cfg, backend
        me = load_identity(cfg.state_dir)
        options = net_options or {}
        self.net = EchoNet(me["secret"], me["public"], me["network_key"], me["net_id"], cfg.node,
                           on_claim=self.on_claim, on_message=self.on_message, **options.get("net", {}))
        self.dropin = DropIn(self.net, self.on_call_ended, **options.get("dropin", {}))
        self.playback = Playback()
        ceiling = cfg.tone_dbfs + 24 if cfg.open_mic else -3.0  # keep speech under the firmware's threshold
        self.pump = AudioPump(backend.send_audio, self.playback,
                              ReplyProcessor(cfg.reply_dbfs, cfg.reply_max_gain_db, ceiling),
                              floor_tone(cfg.tone_dbfs) if cfg.open_mic else None,
                              10 ** (cfg.mic_gain_db / 20), USER_RMS, cfg.barge_in_rms)
        self.transcript = Transcript(cfg.transcript_dir)
        backend.on_audio = self.playback.add
        backend.on_interrupted = self._interrupted
        backend.on_transcript = self._said
        backend.on_closed = self._backend_closed
        self.state = "standby"
        self.ready = True
        self.media_playing = False
        self.session: asyncio.Task | None = None
        self.ended = asyncio.Event()
        self.ended_reason = ""
        self.backend_lost = asyncio.Event()
        self.counters = {"wakes": 0, "sessions": 0, "failed": 0, "interruptions": 0, "reconnects": 0}
        self.last_session: dict = {}
        self._http: aiohttp.ClientSession | None = None

    # ---------------------------------------------------------------- backend events
    def _interrupted(self) -> tuple[str, int]:
        self.counters["interruptions"] += 1
        return self.playback.clear()

    def _said(self, role: str, text: str) -> None:
        log.info("%s: %s", role, text)
        self.transcript.write(role=role, text=text)

    def _backend_closed(self, reason: str) -> None:
        log.warning("voice session closed: %s", reason)
        self.backend_lost.set()

    # ---------------------------------------------------------------- Home Assistant (optional)
    async def _ha(self, method: str, path: str, **kwargs):
        if not self.cfg.ha_url or not self.cfg.ha_token or not self._http:
            return None
        headers = {"Authorization": f"Bearer {self.cfg.ha_token}"}
        timeout = aiohttp.ClientTimeout(total=8)
        async with self._http.request(method, self.cfg.ha_url + path, headers=headers, timeout=timeout, **kwargs) as r:
            return await r.json(content_type=None) if r.status < 300 else None

    async def _ring(self, on: bool) -> None:
        if not self.cfg.ha_light:
            return
        service = "turn_on" if on else "turn_off"
        data = {"entity_id": self.cfg.ha_light}
        if on and self.cfg.ha_light_effect:
            data |= {"effect": self.cfg.ha_light_effect, "brightness": 255}
        try:
            await self._ha("POST", f"/api/services/light/{service}", json=data)
        except Exception as e:
            log.warning("ring: %s", e)

    async def _watch_media(self) -> None:
        """While the Echo plays something, "<wake word>, stop" must keep reaching it."""
        while self.cfg.ha_media_player:
            try:
                state = await self._ha("GET", f"/api/states/{self.cfg.ha_media_player}")
                self.media_playing = bool(state and state.get("state") == "playing")
            except Exception:
                self.media_playing = False
            await asyncio.sleep(2)

    async def _keep_joined(self) -> None:
        """Hands the network key to the Echo through Home Assistant, the path the firmware provides for its owner."""
        told = 0.0
        while True:
            await asyncio.sleep(3)
            out = self.net.outsider(self.cfg.echo_node)
            if not out or self.net.member(self.cfg.echo_node):
                continue
            if out.net and out.net < self.net.net_id:
                log.error("the Echo is in an older network (%016x) and will not move", out.net)
                await asyncio.sleep(REJOIN_S)
            elif time.monotonic() - told >= REJOIN_S:
                told = time.monotonic()
                key = seal_network_key(self.net.secret, self.net.public, out.public, self.net.net_id, self.net.network_key)
                try:
                    await self._ha("POST", f"/api/services/esphome/{self.cfg.ha_key_action}",
                                   json={"network": f"{self.net.net_id:016x}", "key": key})
                    log.info("network key handed to %s through Home Assistant", out.node)
                except Exception as e:
                    log.error("handing over the network key: %s", e)

    # ---------------------------------------------------------------- wake word
    def on_claim(self, peer: Peer, keyword: bytes, score: int, prio: int, won: bool) -> None:
        if peer.node != self.cfg.echo_node or won:
            return
        self.counters["wakes"] += 1
        if self.state != "standby":
            self.net.claim(keyword, WIN_SCORE, 2, True, peer.ip)  # mid-conversation: nothing for Home Assistant
            self.pump.last_user = time.monotonic()
            return
        keep = ("the Echo is busy" if prio >= 2 else "the Echo is playing" if self.media_playing else
                "not ready" if not self.ready else "")
        if keep:
            log.info("leaving the wake word to the Echo: %s", keep)
            self.net.claim(keyword, LOSE_SCORE, 0, False, peer.ip)  # lets it decide now instead of waiting out the round
            return
        log.info("wake word heard (score %d)", score)
        self.net.claim(keyword, WIN_SCORE, 2, True, peer.ip)
        self.start_session(peer)

    def on_message(self, peer: Peer, payload: bytes) -> None:
        if peer.node == self.cfg.echo_node:
            self.dropin.on_message(peer, payload)

    def on_call_ended(self, reason: str) -> None:
        self.ended_reason = reason
        self.ended.set()

    # ---------------------------------------------------------------- session
    def start_session(self, peer: Peer) -> None:
        self.state = "starting"
        self.session = asyncio.create_task(self._session(peer))

    async def _session(self, peer: Peer) -> None:
        started = time.monotonic()
        self.ended.clear()
        self.backend_lost.clear()
        self.counters["sessions"] += 1
        info = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "call_ms": None, "backend_ms": None, "end": ""}
        self.transcript.write(event="started")
        reason = "unknown"
        try:
            connecting = asyncio.create_task(self.backend.start())
            result = await self.dropin.dial(peer)
            info["call_ms"] = round((time.monotonic() - started) * 1000)
            if result != "connected":
                reason = f"call failed: {result}"
                self.counters["failed"] += 1
                connecting.cancel()
                return
            self.pump.start(self.dropin.call)
            log.info("call up after %d ms", info["call_ms"])
            await connecting
            info["backend_ms"] = round((time.monotonic() - started) * 1000)
            self.pump.backend_ready = True
            self.state = "active"
            self.pump.last_user = self.pump.last_reply = time.monotonic()
            log.info("voice session ready after %d ms", info["backend_ms"])
            await self._ring(True)
            reason = await self._converse()
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except Exception as e:
            log.exception("session failed")
            reason = f"error: {e}"
            self.counters["failed"] += 1
        finally:
            self.state = "ending"
            await self._ring(False)
            self.dropin.hangup(reason)
            self.pump.stop()
            self.playback.clear()
            await self.backend.stop()
            info |= {"end": reason, "seconds": round(time.monotonic() - started), "levels": self.pump.level_report()}
            self.last_session = info
            self.transcript.write(event="ended", reason=reason, seconds=info["seconds"])
            self.state = "standby"
            log.info("standby (%d s: %s), levels %s", info["seconds"], reason, info["levels"])

    async def _converse(self) -> str:
        attempts = 0
        while True:
            try:
                await asyncio.wait_for(self.ended.wait(), 1.0)
                return self.ended_reason or "hung up on the Echo"
            except asyncio.TimeoutError:
                pass
            call, now = self.dropin.call, time.monotonic()
            if not call:
                return "call gone"
            if now - call.last_rx > LOST_AFTER_S:
                return "lost the Echo"
            idle = now - max(self.pump.last_user, self.pump.last_reply)
            if idle > self.cfg.idle_s:
                return f"{round(idle)} s of silence"
            if self.backend_lost.is_set():
                if attempts >= len(RECONNECT_DELAYS_S):
                    self.counters["failed"] += 1
                    return "voice session could not be restored"
                self.pump.backend_ready = False  # keep buffering meanwhile
                await asyncio.sleep(RECONNECT_DELAYS_S[attempts])
                attempts += 1
                self.backend_lost.clear()
                self.counters["reconnects"] += 1
                try:
                    await self.backend.stop()
                    await self.backend.start()
                    self.pump.backend_ready = True
                    attempts = 0
                    log.info("voice session restored")
                except Exception as e:
                    log.warning("reconnect %d failed: %s", attempts, e)
                    self.backend_lost.set()

    # ---------------------------------------------------------------- status
    def status(self) -> dict:
        echo = self.net.member(self.cfg.echo_node)
        call = self.dropin.call
        return {"state": self.state, "ready": self.ready, "media_playing": self.media_playing,
                "echo": {"ip": echo.ip, "seen_s": round(time.monotonic() - echo.seen)} if echo else None,
                "counters": self.counters, "last_session": self.last_session, "idle_s": self.cfg.idle_s,
                "call": call.stats if call else None, "mic_backlog_frames": len(call.rx) if call else 0}

    async def _status_server(self) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request = (await asyncio.wait_for(reader.readline(), 3)).decode("latin1").split()
                while (await asyncio.wait_for(reader.readline(), 3)) not in (b"\r\n", b"\n", b""):
                    pass
                body, code = self._route(request[0], request[1])
                data = json.dumps(body, ensure_ascii=False).encode()
                head = f"HTTP/1.1 {code} X\r\nContent-Type: application/json\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n"
                writer.write(head.encode() + data)
                await writer.drain()
            except Exception as e:
                log.debug("status request: %s", e)
            finally:
                writer.close()
        server = await asyncio.start_server(handle, "127.0.0.1", self.cfg.status_port)
        async with server:
            await server.serve_forever()

    def _route(self, method: str, path: str) -> tuple[dict, int]:
        if method == "GET" and path == "/status":
            return self.status(), 200
        if method == "POST" and path == "/call":
            echo = self.net.member(self.cfg.echo_node)
            if self.state != "standby" or not echo:
                return {"error": "not ready", **self.status()}, 409
            self.start_session(echo)
            return {"ok": True}, 200
        if method == "POST" and path == "/hangup":
            self.on_call_ended("ended locally")
            return {"ok": True}, 200
        return {"error": "not found"}, 404

    async def run(self) -> None:
        self._http = aiohttp.ClientSession()
        await self.net.start()
        await self.dropin.start()
        log.info("network %016x, waiting for %s", self.net.net_id, self.cfg.echo_node)
        await self._ring(False)
        await asyncio.gather(self._status_server(), self._watch_media(), self._keep_joined())

    async def close(self) -> None:
        if self.session and not self.session.done():
            self.on_call_ended("shutting down")
            await asyncio.wait({self.session}, timeout=5)
        await asyncio.sleep(0.4)  # lets the bye messages leave
        self.net.close()
        if self._http:
            await self._http.close()


