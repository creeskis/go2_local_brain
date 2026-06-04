"""Entry point: connect to the Go2 and run the REPL."""

from __future__ import annotations

import asyncio
import logging
import sys

from .brain.local_llm import LocalRobotBrain
from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient


async def _amain() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

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
