"""The interface between the bridge and whatever provides the conversation."""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable


class VoiceBackend(ABC):
    """All audio is 16 kHz, 16-bit mono PCM. The callbacks run on the event loop, except that send_audio may be
    called from the audio thread."""

    on_audio: Callable[[str, bytes], None]        # (item id, pcm) reply audio to play
    on_interrupted: Callable[[], tuple[str, int]]  # user spoke over a reply: stop playback, returns (item id, heard ms)
    on_transcript: Callable[[str, str], None]     # (role "user" | "assistant", text)
    on_closed: Callable[[str], None]              # the session ended without stop() being called

    @abstractmethod
    async def start(self) -> None:
        """Connects. Returns once audio can be accepted. Raises on failure."""

    @abstractmethod
    async def stop(self) -> None:
        """Ends the session."""

    @abstractmethod
    def send_audio(self, pcm: bytes) -> None:
        """Queues one microphone frame. Must not block."""
