import math

import numpy as np
import opuslib
import opuslib.api.ctl as ctl
import opuslib.api.encoder as enc

from echo_voice_bridge.audio import FRAME_BYTES, Playback, ReplyProcessor, apply_gain, dbfs, floor_tone, rms

TONE_DB = -36.0
CEILING = TONE_DB + 24


def voice(amp, frames):
    t = np.arange(320 * frames) / 16000
    v = amp * np.sin(2 * np.pi * 220 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 3 * t)) + 0.4 * amp * np.sin(2 * np.pi * 1800 * t)
    return v.astype("<i2").tobytes()


def codec():
    e = opuslib.Encoder(16000, 1, opuslib.APPLICATION_AUDIO)
    enc.encoder_ctl(e.encoder_state, ctl.set_bitrate, 48000)
    return e, opuslib.Decoder(16000, 1)


def test_floor_tone_survives_opus():
    tone = floor_tone(TONE_DB)
    assert len(tone) == FRAME_BYTES
    e, d = codec()
    levels = [dbfs(rms(d.decode(e.encode(tone, 320), 320))) for _ in range(60)][20:]
    assert all(abs(level - TONE_DB) < 1.5 for level in levels)


def test_speech_stays_under_the_firmware_threshold_at_any_input_level():
    tone = np.frombuffer(floor_tone(TONE_DB), dtype="<i2").astype(np.int32)
    for amp in (1200, 5000, 22000):
        p, (e, d) = ReplyProcessor(-17, 14, CEILING), codec()
        pcm = voice(amp, 150)
        levels = []
        for i in range(150):
            frame = np.frombuffer(p.run(pcm[i * 640:(i + 1) * 640]), dtype="<i2").astype(np.int32) + tone
            levels.append(dbfs(rms(d.decode(e.encode(frame.astype("<i2").tobytes(), 320), 320))))
        assert max(levels[75:]) - TONE_DB < 25


def test_compressor_lifts_quiet_speech_and_has_no_steps():
    p = ReplyProcessor(-17, 14, CEILING)
    pcm = voice(1200, 200)
    out = b"".join(p.run(pcm[i * 640:(i + 1) * 640]) for i in range(200))
    half = len(out) // 2
    assert dbfs(rms(out[half:])) > dbfs(rms(pcm[half:])) + 8
    x = np.frombuffer(out[half:], dtype="<i2").astype(float)
    assert np.max(np.abs(np.diff(x))) / 32768 < 0.2


def test_mic_gain_limits_softly():
    loud = apply_gain(voice(22000, 5), 10 ** (12 / 20))
    assert np.abs(np.frombuffer(loud, dtype="<i2")).max() <= 28001
    quiet = voice(500, 5)
    assert math.isclose(dbfs(rms(apply_gain(quiet, 10 ** (12 / 20)))) - dbfs(rms(quiet)), 12, abs_tol=0.5)


def test_playback_tracks_what_was_heard_per_item():
    p = Playback()
    p.add("a", bytes(FRAME_BYTES * 3))
    p.add("b", bytes(FRAME_BYTES * 2))
    for _ in range(4):
        assert p.next_frame() is not None
    assert p.clear() == ("b", 20)
    assert p.next_frame() is None and not p.pending
