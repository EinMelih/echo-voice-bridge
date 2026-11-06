"""The Realtime backend against a local stand-in for the API."""
import asyncio
import base64

import numpy as np
import pytest
from aiohttp import WSMsgType, web

from echo_voice_bridge.backends.openai_realtime import OpenAIRealtime


class FakeAPI:
    def __init__(self, reject=False):
        self.events, self.socket, self.reject = [], None, reject

    async def handler(self, request):
        self.headers, self.query = request.headers, request.query
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.socket = ws
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                break
            event = msg.json()
            self.events.append(event)
            if event["type"] == "session.update":
                if self.reject:
                    await ws.send_json({"type": "error", "error": {"code": "bad", "message": "no access"}})
                else:
                    await ws.send_json({"type": "session.updated"})
        return ws

    def of(self, kind):
        return [e for e in self.events if e["type"] == kind]


def run(test, reject=False, **options):
    async def main():
        api = FakeAPI(reject)
        app = web.Application()
        app.router.add_get("/", api.handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        rt = OpenAIRealtime("key", "test-model", "marin", "Be brief.", url=f"http://127.0.0.1:{port}/", **options)
        rt.audio, rt.said, rt.closed = [], [], []
        rt.on_audio = lambda item, pcm: rt.audio.append((item, pcm))
        rt.on_interrupted = lambda: ("item1", 700)
        rt.on_transcript = lambda role, text: rt.said.append((role, text))
        rt.on_closed = rt.closed.append
        try:
            await asyncio.wait_for(test(rt, api), 10)
        finally:
            await rt.stop()
            await runner.cleanup()
    asyncio.run(main())


async def until(check):
    for _ in range(200):
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met in time")


def test_session_setup_uses_24khz_pcm_and_authenticates():
    async def test(rt, api):
        await rt.start()
        assert api.headers["Authorization"] == "Bearer key" and api.query["model"] == "test-model"
        session = api.of("session.update")[0]["session"]
        assert session["audio"]["input"]["format"] == {"type": "audio/pcm", "rate": 24000}
        assert session["audio"]["output"]["voice"] == "marin"
        assert session["audio"]["input"]["turn_detection"]["interrupt_response"] is True
    run(test)


def test_microphone_is_resampled_up_and_replies_down():
    async def test(rt, api):
        await rt.start()
        for _ in range(50):
            rt.send_audio(np.zeros(320, dtype="<i2").tobytes())
        await until(lambda: len(api.of("input_audio_buffer.append")) > 30)
        sent = sum(len(base64.b64decode(e["audio"])) // 2 for e in api.of("input_audio_buffer.append"))
        assert 0.8 * 24000 < sent <= 24000 * 1.01

        reply = (np.zeros(24000, dtype="<i2")).tobytes()
        for i in range(0, len(reply), 4800):
            await api.socket.send_json({"type": "response.output_audio.delta", "item_id": "r1",
                                        "delta": base64.b64encode(reply[i:i + 4800]).decode()})
        await until(lambda: sum(len(p) for _, p in rt.audio) > 0.8 * 32000)
        assert {item for item, _ in rt.audio} == {"r1"}
    run(test)


def test_interruption_truncates_the_reply_to_what_was_heard():
    async def test(rt, api):
        await rt.start()
        await api.socket.send_json({"type": "input_audio_buffer.speech_started", "item_id": "u1", "audio_start_ms": 10})
        await until(lambda: api.of("conversation.item.truncate"))
        assert api.of("conversation.item.truncate")[0] == {"type": "conversation.item.truncate", "item_id": "item1",
                                                           "content_index": 0, "audio_end_ms": 700}
    run(test)


def test_transcripts_are_reported_and_kept_as_context():
    async def test(rt, api):
        await rt.start()
        await api.socket.send_json({"type": "conversation.item.input_audio_transcription.completed", "transcript": " Hi there "})
        await api.socket.send_json({"type": "response.output_audio_transcript.done", "transcript": "Hello."})
        await until(lambda: len(rt.said) == 2)
        assert rt.said == [("user", "Hi there"), ("assistant", "Hello.")]
        instructions = rt.session_config()["instructions"]
        assert "User: Hi there" in instructions and "You: Hello." in instructions
    run(test)


def test_unexpected_close_is_reported_but_a_requested_stop_is_not():
    async def test(rt, api):
        await rt.start()
        await api.socket.close()
        await until(lambda: rt.closed)
    run(test)

    async def test_stop(rt, api):
        await rt.start()
        await rt.stop()
        await asyncio.sleep(0.05)
        assert rt.closed == []
    run(test_stop)


def test_api_error_during_setup_fails_the_start():
    async def test(rt, api):
        with pytest.raises(ConnectionError, match="no access"):
            await rt.start()
    run(test, reject=True)
