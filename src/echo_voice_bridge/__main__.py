from __future__ import annotations

import asyncio
import logging
import signal

from .backends.openai_realtime import OpenAIRealtime
from .bridge import Bridge
from .config import Config


async def amain(cfg: Config) -> None:
    backend = OpenAIRealtime(cfg.openai_key, cfg.model, cfg.voice, cfg.instructions, cfg.language, cfg.vad, cfg.eagerness)
    bridge = Bridge(cfg, backend)
    runner = asyncio.create_task(bridge.run())
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, runner.cancel)
    try:
        await runner
    except asyncio.CancelledError:
        logging.getLogger(__name__).info("shutting down")
    finally:
        await bridge.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    cfg = Config()
    if not cfg.echo_node or not cfg.openai_key:
        raise SystemExit("EVB_ECHO_NODE and OPENAI_API_KEY must be set (see .env.example)")
    asyncio.run(amain(cfg))


if __name__ == "__main__":
    main()
