"""Optional JSONL record of conversations. Off unless EVB_TRANSCRIPT_DIR is set."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)


class Transcript:
    def __init__(self, directory: str):
        self.path = Path(directory) / "conversations.jsonl" if directory else None

    def write(self, **record) -> None:
        if not self.path:
            return
        record = {"time": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **record}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as e:
            log.error("transcript not writable: %s", e)
