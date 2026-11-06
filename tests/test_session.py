"""Whole sessions against the simulated Echo."""
import asyncio

import numpy as np

SPEECH = (8000 * np.sin(2 * np.pi * 300 * np.arange(320) / 16000)).astype("<i2").tobytes()


async def until(check, timeout=5.0):
    for _ in range(int(timeout / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")


def test_wake_word_wins_the_round_before_the_call_starts(harness):
    """Regression: a call invite with a higher counter must not overtake the claim, or the Echo rejects the claim as
    a replay and starts its own voice pipeline as well."""
    async def test(bridge, echo, backend):
        echo.wake()
        await until(lambda: "invite" in echo.events)
        await asyncio.wait_for(echo.won.wait(), 1)
        assert echo.events.index("claim-won") < echo.events.index("invite")
    harness(test)


def test_conversation_flows_both_ways_and_ends_when_the_echo_hangs_up(harness):
    async def test(bridge, echo, backend):
        echo.wake()
        await until(lambda: bridge.state == "active" and echo.key)
        for _ in range(20):
            echo.send_mic(SPEECH)
            await asyncio.sleep(0.02)
        await until(lambda: backend.mic)
        assert max(np.abs(np.frombuffer(f, dtype="<i2")).max() for f in backend.mic) > 2000

        backend.on_audio("item1", SPEECH * 25)
        await until(lambda: len(echo.received) > 40)
        heard = max(np.abs(np.frombuffer(f, dtype="<i2")).max() for f in echo.received)
        assert heard > 2000

        echo.hang_up()
        await until(lambda: bridge.state == "standby")
        assert backend.stopped == 1
        assert bridge.last_session["end"] == "stop"
        assert bridge.last_session["call_ms"] is not None
    harness(test)


def test_call_stays_alive_on_silence_with_the_floor_tone(harness):
    async def test(bridge, echo, backend):
        echo.wake()
        await until(lambda: bridge.state == "active" and echo.key)
        await until(lambda: len(echo.received) > 30)
        levels = [float(np.sqrt(np.mean(np.frombuffer(f, dtype="<i2").astype(float) ** 2))) for f in echo.received[10:]]
        assert min(levels) > 200  # about -36 dBFS, what lifts the firmware's floor
    harness(test)


def test_interruption_stops_playback_and_reports_what_was_heard(harness):
    async def test(bridge, echo, backend):
        echo.wake()
        await until(lambda: bridge.state == "active")
        backend.on_audio("item7", SPEECH * 100)
        await asyncio.sleep(0.3)
        item, heard_ms = backend.on_interrupted()
        assert item == "item7"
        assert 100 <= heard_ms <= 800
        assert not bridge.playback.pending
    harness(test)


def test_wake_word_stays_with_the_echo_when_it_is_busy(harness):
    async def test(bridge, echo, backend):
        echo.wake(prio=2)  # a ringing timer or a running reply
        await until(lambda: "claim" in echo.events)
        assert "claim-won" not in echo.events and bridge.state == "standby"
    harness(test)


def test_wake_word_stays_with_the_echo_while_it_plays_media(harness):
    async def test(bridge, echo, backend):
        bridge.media_playing = True
        echo.wake()
        await until(lambda: "claim" in echo.events)
        assert "claim-won" not in echo.events and bridge.state == "standby"
    harness(test)


def test_refused_call_returns_to_standby(harness):
    async def test(bridge, echo, backend):
        echo.wake()
        await until(lambda: bridge.last_session.get("end", "").startswith("call failed"))
        assert "do not disturb" in bridge.last_session["end"]
        assert bridge.state == "standby" and bridge.counters["failed"] == 1
    harness(test, refuse="do not disturb is on")


def test_silence_ends_the_session(harness):
    async def test(bridge, echo, backend):
        echo.wake()
        await until(lambda: bridge.state == "standby" and bridge.last_session, timeout=8)
        assert "silence" in bridge.last_session["end"]
    harness(test, idle_s=0.5)


def test_lost_voice_session_is_restored_within_the_call(harness):
    async def test(bridge, echo, backend):
        echo.wake()
        await until(lambda: bridge.state == "active")
        for _ in range(3):
            echo.send_mic(SPEECH)
        backend.on_closed("network dropped")
        async def keep_call_alive():
            while True:
                echo.send_mic(bytes(640))
                await asyncio.sleep(0.1)
        keeper = asyncio.create_task(keep_call_alive())
        await until(lambda: backend.started == 2, timeout=6)
        await until(lambda: bridge.pump.backend_ready)
        keeper.cancel()
        assert bridge.state == "active" and bridge.counters["reconnects"] == 1
    harness(test)
