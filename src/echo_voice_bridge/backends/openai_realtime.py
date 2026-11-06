"""OpenAI Realtime API over WebSocket (https://developers.openai.com/api/docs/guides/realtime-conversations).

The API only takes 24 kHz PCM. Over WebSocket the client has to truncate an interrupted reply itself, since only it
knows how much was actually heard. Sessions keep no memory, so recent turns are passed along as context.
"""
from __future__ import annotations

import asyncio
import base64
import logging

import aiohttp
import numpy as np
import soxr

from .base import VoiceBackend

log = logging.getLogger(__name__)

URL = "wss://api.openai.com/v1/realtime"
API_RATE, BRIDGE_RATE = 24000, 16000


class OpenAIRealtime(VoiceBackend):
    def __init__(self, api_key: str, model: str, voice: str, instructions: str, language: str = "",
                 vad: str = "semantic_vad", eagerness: str = "auto", transcribe_model: str = "gpt-4o-mini-transcribe",
                 url: str = URL):
        self.api_key, self.model, self.voice, self.instructions = api_key, model, voice, instructions
        self.language, self.vad, self.eagerness, self.transcribe_model = language, vad, eagerness, transcribe_model
        self.url = url
        self.context: list[tuple[str, str]] = []
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._reader: asyncio.Task | None = None
        self._writer: asyncio.Task | None = None
        self._outbox: asyncio.Queue[dict] = asyncio.Queue()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ready: asyncio.Future | None = None
        self._stopping = False
        self._up = self._down = None

    def session_config(self) -> dict:
        instructions = self.instructions
        if self.context:
            lines = "\n".join(f"{'User' if r == 'user' else 'You'}: {t}" for r, t in self.context)
            instructions += f"\n\nEarlier in this conversation:\n{lines}"
        turn = {"type": self.vad, "create_response": True, "interrupt_response": True}
        if self.vad == "semantic_vad":
            turn["eagerness"] = self.eagerness
        transcription = {"model": self.transcribe_model}
        if self.language:
            transcription["language"] = self.language
        return {
            "type": "realtime",
            "model": self.model,
            "output_modalities": ["audio"],
            "instructions": instructions,
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": API_RATE}, "transcription": transcription, "turn_detection": turn},
                "output": {"format": {"type": "audio/pcm", "rate": API_RATE}, "voice": self.voice},
            },
        }

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stopping = False
        self._outbox = asyncio.Queue()
        self._ready = self._loop.create_future()
        self._up = soxr.ResampleStream(BRIDGE_RATE, API_RATE, 1, dtype="int16", quality="HQ")
        self._down = soxr.ResampleStream(API_RATE, BRIDGE_RATE, 1, dtype="int16", quality="HQ")
        self._http = aiohttp.ClientSession()
        try:
            self._ws = await self._http.ws_connect(f"{self.url}?model={self.model}", heartbeat=20,
                                                   headers={"Authorization": f"Bearer {self.api_key}"})
            await self._ws.send_json({"type": "session.update", "session": self.session_config()})
            self._reader = asyncio.create_task(self._read())
            self._writer = asyncio.create_task(self._write())
            await asyncio.wait_for(asyncio.shield(self._ready), 10)
        except Exception:
            await self.stop()
            raise

    async def stop(self) -> None:
        self._stopping = True
        for task in (self._reader, self._writer):
            if task and task is not asyncio.current_task():
                task.cancel()
        self._reader = self._writer = None
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._http and not self._http.closed:
            await self._http.close()
        self._ws = self._http = None

    def send_audio(self, pcm: bytes) -> None:
        if self._loop is None or self._up is None:
            return
        samples = self._up.resample_chunk(np.frombuffer(pcm, dtype="<i2"))
        if samples.size:
            event = {"type": "input_audio_buffer.append", "audio": base64.b64encode(samples.astype("<i2").tobytes()).decode()}
            self._loop.call_soon_threadsafe(self._outbox.put_nowait, event)

    async def _write(self) -> None:
        while True:
            await self._ws.send_json(await self._outbox.get())

    async def _read(self) -> None:
        reason = "connection closed"
        try:
            async for msg in self._ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._handle(msg.json())
                elif msg.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSED):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            reason = f"read failed: {e}"
            log.exception("realtime read")
        if self._ready and not self._ready.done():
            self._ready.set_exception(ConnectionError(reason))
        if not self._stopping:
            self.on_closed(reason)

    async def _handle(self, event: dict) -> None:
        kind = event.get("type", "")
        if kind == "session.updated":
            if self._ready and not self._ready.done():
                self._ready.set_result(True)
        elif kind == "response.output_audio.delta":
            pcm = np.frombuffer(base64.b64decode(event["delta"]), dtype="<i2")
            out = self._down.resample_chunk(pcm)
            if out.size:
                self.on_audio(event.get("item_id", ""), out.astype("<i2").tobytes())
        elif kind == "input_audio_buffer.speech_started":
            item, heard_ms = self.on_interrupted()
            if item and heard_ms:
                await self._ws.send_json({"type": "conversation.item.truncate", "item_id": item,
                                          "content_index": 0, "audio_end_ms": heard_ms})
        elif kind == "conversation.item.input_audio_transcription.completed":
            self._remember("user", event.get("transcript", ""))
        elif kind == "response.output_audio_transcript.done":
            self._remember("assistant", event.get("transcript", ""))
        elif kind == "error":
            error = event.get("error", {})
            log.error("realtime error %s: %s", error.get("code"), error.get("message"))
            if self._ready and not self._ready.done():
                self._ready.set_exception(ConnectionError(error.get("message", "error")))

    def _remember(self, role: str, text: str) -> None:
        text = text.strip()
        if text:
            self.context = (self.context + [(role, text)])[-20:]
            self.on_transcript(role, text)
