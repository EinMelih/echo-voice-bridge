"""The 20 ms audio clock between a call and a voice backend, and the signal processing around it."""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import deque
from collections.abc import Callable

import numpy as np

log = logging.getLogger(__name__)

FRAME_BYTES = 640  # 20 ms, 16 kHz, 16-bit mono
FRAME_MS = 20
SILENCE = bytes(FRAME_BYTES)


def rms(pcm: bytes) -> float:
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64)
    return float(np.sqrt(np.mean(x * x))) if x.size else 0.0


def dbfs(value: float) -> float:
    return 20 * math.log10(max(value, 1.0) / 32768)


def floor_tone(level_dbfs: float) -> bytes:
    """One period of 50 Hz per frame, so frames join seamlessly; the Echo's small speaker barely reproduces it.

    The firmware drops the microphone by 25 dB when a played frame is over 25 dB above the quietest recent one
    (dropin.c far_voice). Even a short "mhm" from the assistant would cut the user off for a second, which the
    assistant then takes for the end of the sentence. A steady floor keeps speech under that threshold.
    """
    amp = 32768 * 10 ** (level_dbfs / 20) * math.sqrt(2)
    return np.round(amp * np.sin(2 * np.pi * np.arange(320) / 320)).astype("<i2").tobytes()


class Biquad:
    """RBJ high-pass: removes what a small speaker cannot reproduce and would only distort on."""

    def __init__(self, cutoff_hz: float, q: float = 0.7, rate: int = 16000):
        w = 2 * math.pi * cutoff_hz / rate
        alpha, cos = math.sin(w) / (2 * q), math.cos(w)
        a0 = 1 + alpha
        self.b = ((1 + cos) / 2 / a0, -(1 + cos) / a0, (1 + cos) / 2 / a0)
        self.a = (-2 * cos / a0, (1 - alpha) / a0)
        self.state = (0.0, 0.0, 0.0, 0.0)

    def run(self, x: np.ndarray) -> np.ndarray:
        b0, b1, b2 = self.b
        a1, a2 = self.a
        x1, x2, y1, y2 = self.state
        out = np.empty_like(x)
        for i, v in enumerate(x.tolist()):
            y = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
            x2, x1, y2, y1 = x1, v, y1, y
            out[i] = y
        self.state = (x1, x2, y1, y2)
        return out


class ReplyProcessor:
    """High-pass, compressor and soft limit. Gain ramps within a frame: a step per frame buzzes at 50 Hz."""

    def __init__(self, target_dbfs: float, max_gain_db: float, ceiling_dbfs: float, highpass_hz: float = 120.0):
        self.target = 32768 * 10 ** (target_dbfs / 20)
        self.max_gain = 10 ** (max_gain_db / 20)
        self.ceiling = 32768 * 10 ** (ceiling_dbfs / 20) * 0.95
        self.hp = Biquad(highpass_hz)
        self.gain = 1.0

    def run(self, pcm: bytes) -> bytes:
        x = self.hp.run(np.frombuffer(pcm, dtype="<i2").astype(np.float64))
        level = float(np.sqrt(np.mean(x * x))) + 1e-9
        want = self.gain
        if level > 60:  # only regulate on speech; never pull pauses up
            want = min(self.max_gain, self.target / level, self.ceiling / level)
        next_gain = want if want < self.gain else self.gain + (want - self.gain) * 0.05  # fast attack, slow release
        y = x * np.linspace(self.gain, next_gain, x.size, endpoint=False)
        self.gain = next_gain
        limit = 30000.0
        return np.clip(limit * np.tanh(y / limit), -32768, 32767).astype("<i2").tobytes()


def apply_gain(pcm: bytes, gain: float) -> bytes:
    limit = 28000.0
    x = np.frombuffer(pcm, dtype="<i2").astype(np.float64) * gain
    return (limit * np.tanh(x / limit)).astype("<i2").tobytes()


class Playback:
    """Reply audio waiting to be played. Knows how much of each backend item was played, for truncation."""

    def __init__(self):
        self._lock = threading.Lock()
        self._queue: deque[tuple[str, bytearray]] = deque()
        self.current_item = ""
        self.current_played = 0

    def add(self, item_id: str, pcm: bytes) -> None:
        with self._lock:
            if self._queue and self._queue[-1][0] == item_id:
                self._queue[-1][1].extend(pcm)
            else:
                self._queue.append((item_id, bytearray(pcm)))

    def next_frame(self) -> bytes | None:
        with self._lock:
            while self._queue:
                item, buf = self._queue[0]
                if len(buf) >= FRAME_BYTES or len(self._queue) > 1:
                    frame = bytes(buf[:FRAME_BYTES])
                    del buf[:FRAME_BYTES]
                    if item != self.current_item:
                        self.current_item, self.current_played = item, 0
                    self.current_played += len(frame)
                    if not buf and len(self._queue) > 1:
                        self._queue.popleft()
                    return frame.ljust(FRAME_BYTES, b"\0")
                return None
            return None

    def clear(self) -> tuple[str, int]:
        """Stops playback. Returns the item that was playing and how many milliseconds of it were heard."""
        with self._lock:
            self._queue.clear()
            return self.current_item, self.current_played * FRAME_MS // FRAME_BYTES

    @property
    def pending(self) -> bool:
        with self._lock:
            return any(buf for _, buf in self._queue)


class AudioPump:
    """Drives both directions every 20 ms, silence included: the stream is what keeps the call alive."""

    def __init__(self, send_to_backend: Callable[[bytes], None], playback: Playback, processor: ReplyProcessor,
                 tone: bytes | None, mic_gain: float, user_rms: float, barge_in_rms: float):
        self.send_to_backend, self.playback, self.processor = send_to_backend, playback, processor
        self.tone, self.mic_gain, self.user_rms, self.barge_in_rms = tone, mic_gain, user_rms, barge_in_rms
        self.call = None
        self.backend_ready = False
        self.last_user = self.last_reply = time.monotonic()
        self.levels: dict[str, list[float]] = {"mic": [], "reply": []}
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self, call) -> None:
        self.call = call
        self.backend_ready = False
        self.last_user = self.last_reply = time.monotonic()
        self.levels = {"mic": [], "reply": []}
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="audio", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)
        self.call = None

    def level_report(self) -> dict:
        out = {}
        for name, values in self.levels.items():
            if values:
                ordered = sorted(values)
                out[name] = {"p50": round(dbfs(ordered[len(ordered) // 2]), 1),
                             "p95": round(dbfs(ordered[int(len(ordered) * 0.95)]), 1), "frames": len(ordered)}
        return out

    def step(self) -> None:
        """One tick. Separate from the loop so tests can drive it without a clock."""
        call, now = self.call, time.monotonic()
        if call is None:
            return
        self._uplink(call, now)
        self._downlink(call, now)

    def _uplink(self, call, now: float) -> None:
        if not self.backend_ready or not call.rx:
            return  # stays buffered until the backend is ready, so nothing said during setup is lost
        frame = call.rx.popleft()
        level = rms(frame)
        while len(call.rx) > 3 and level < self.user_rms:  # catch up during pauses, never during speech
            frame = call.rx.popleft()
            level = rms(frame)
        replying = self.playback.pending or now - self.last_reply < 0.3
        if replying and level < self.barge_in_rms:
            frame = SILENCE  # what the Echo's canceller left of the reply must not interrupt the reply
        elif level > self.user_rms:
            self.last_user = now
            self.levels["mic"].append(level)
        self.send_to_backend(frame if frame is SILENCE else apply_gain(frame, self.mic_gain))

    def _downlink(self, call, now: float) -> None:
        frame = self.playback.next_frame()
        if frame is not None:
            level = rms(frame)
            if level > 60:
                self.last_reply = now
                self.levels["reply"].append(level)
            frame = self.processor.run(frame)
        else:
            frame = SILENCE
        if self.tone is not None:
            mixed = np.frombuffer(frame, dtype="<i2").astype(np.int32) + np.frombuffer(self.tone, dtype="<i2")
            frame = np.clip(mixed, -32768, 32767).astype("<i2").tobytes()
        call.send(frame)

    def _loop(self) -> None:
        next_tick = time.perf_counter()
        while self._running:
            next_tick += 0.02
            try:
                self.step()
            except Exception:
                log.exception("audio tick failed")
            delay = next_tick - time.perf_counter()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.2:
                next_tick = time.perf_counter()  # stalled: do not race to catch up
