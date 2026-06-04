"""Entry point: connect to the Go2 and run the REPL."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from .brain.local_llm import LocalRobotBrain
from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient


def _configure_logging() -> None:
    """Keep robot-brain logs visible without flooding the REPL."""
    verbose = os.getenv("VERBOSE_WEBRTC_LOGS", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    root_level = logging.INFO if verbose else logging.WARNING
    logging.basicConfig(
        level=root_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("go2_local_brain").setLevel(logging.INFO)


async def _amain() -> int:
    _configure_logging()

    cfg = load_config()
    client = Go2WebRTCClient(
        Go2Config(
            ip=cfg.go2_ip,
            aes_128_key=cfg.go2_aes_128_key,
            force_motion_mode=cfg.force_motion_mode,
            enable_exploration=cfg.enable_exploration,
            exploration_min_obstacle_m=cfg.exploration_min_obstacle_m,
            exploration_mode=cfg.exploration_mode,
            exploration_max_duration_s=cfg.exploration_max_duration_s,
        )
    )

    try:
        try:
            await client.connect()
        except Exception as exc:  # noqa: BLE001
            logging.error("failed to connect to Go2 at %s: %s", cfg.go2_ip, exc)
            return 1
        brain = LocalRobotBrain(client, model=cfg.ollama_model)
        try:
            await brain.repl()
        except KeyboardInterrupt:
            pass
    finally:
        await client.close()
    return 0


def main() -> None:
    try:
        rc = asyncio.run(_amain())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)


if __name__ == "__main__":
    main()
