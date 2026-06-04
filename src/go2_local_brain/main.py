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
    """Keep robot-brain logs visible without flooding the REPL.

    unitree_webrtc_connect logs every incoming sport-state packet via the
    root logger at INFO. That is useful while debugging WebRTC, but it makes
    the prompt nearly unusable once telemetry starts streaming. By default we
    keep third-party/root logs at WARNING and our package logs at INFO.
    Set VERBOSE_WEBRTC_LOGS=1 to restore full upstream INFO logging.
    """
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
        )
    )

    # One try/finally around both connect() and repl() so a partial
    # connect still gets torn down (the upstream package opens a peer
    # connection during connect() before the data-channel handshake;
    # leaking it leaves WebRTC sessions on the robot side).
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
            # User hit Ctrl-C; fall through to clean shutdown.
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
