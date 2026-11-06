"""Configuration from the environment, and the bridge's identity on the Echo network."""
from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from pathlib import Path

from .echonet import x25519_keypair

DEFAULT_INSTRUCTIONS = ("You are a voice assistant speaking through a small smart speaker. Keep answers short and "
                        "natural, like a spoken conversation, unless asked for more.")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"EVB_{name}", default)


@dataclass
class Config:
    echo_node: str = field(default_factory=lambda: _env("ECHO_NODE"))
    node: str = field(default_factory=lambda: _env("NODE", "voice-bridge"))
    state_dir: Path = field(default_factory=lambda: Path(_env("STATE_DIR", "/state")))
    transcript_dir: str = field(default_factory=lambda: _env("TRANSCRIPT_DIR"))
    status_port: int = field(default_factory=lambda: int(_env("STATUS_PORT", "8099")))
    idle_s: float = field(default_factory=lambda: float(_env("IDLE_S", "120")))

    openai_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    model: str = field(default_factory=lambda: _env("MODEL", "gpt-realtime-2.1"))
    voice: str = field(default_factory=lambda: _env("VOICE", "marin"))
    language: str = field(default_factory=lambda: _env("LANGUAGE"))
    vad: str = field(default_factory=lambda: _env("VAD", "semantic_vad"))
    eagerness: str = field(default_factory=lambda: _env("EAGERNESS", "auto"))
    instructions: str = field(default_factory=lambda: _env("INSTRUCTIONS", DEFAULT_INSTRUCTIONS))

    open_mic: bool = field(default_factory=lambda: _env("OPEN_MIC", "1") == "1")
    tone_dbfs: float = field(default_factory=lambda: float(_env("TONE_DBFS", "-36")))
    reply_dbfs: float = field(default_factory=lambda: float(_env("REPLY_DBFS", "-17")))
    reply_max_gain_db: float = field(default_factory=lambda: float(_env("REPLY_MAX_GAIN_DB", "14")))
    mic_gain_db: float = field(default_factory=lambda: float(_env("MIC_GAIN_DB", "12")))
    barge_in_rms: float = field(default_factory=lambda: float(_env("BARGE_IN_RMS", "400")))

    ha_url: str = field(default_factory=lambda: _env("HA_URL").rstrip("/"))
    ha_token: str = field(default_factory=lambda: _env("HA_TOKEN"))
    ha_media_player: str = field(default_factory=lambda: _env("HA_MEDIA_PLAYER"))
    ha_light: str = field(default_factory=lambda: _env("HA_LIGHT"))
    ha_light_effect: str = field(default_factory=lambda: _env("HA_LIGHT_EFFECT", "Rainbow"))

    @property
    def ha_key_action(self) -> str:
        return self.echo_node.replace("-", "_") + "_arbitration_key"


def load_identity(state_dir: Path) -> dict:
    """Created once, then only read. The network key lets its holder open the Echo's microphone: keep the file private."""
    path = state_dir / "identity.json"
    if path.exists():
        data = json.loads(path.read_text())
        return {k: (bytes.fromhex(v) if k in ("secret", "public", "network_key") else v) for k, v in data.items()}
    secret, public = x25519_keypair()
    # An Echo only moves to a network with a lower id, so stay well below any random 64-bit id
    identity = {"secret": secret, "public": public, "network_key": os.urandom(32), "net_id": random.randrange(1 << 48, 1 << 56)}
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.touch(mode=0o600)
    tmp.write_text(json.dumps({k: (v.hex() if isinstance(v, bytes) else v) for k, v in identity.items()}))
    tmp.rename(path)
    return identity
