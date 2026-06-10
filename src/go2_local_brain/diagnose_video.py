"""Diagnose whether Go2 WebRTC video frames arrive."""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from typing import Any

from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient


async def _amain() -> int:
    parser = argparse.ArgumentParser(description="Connect to the Go2 and count WebRTC video frames.")
    parser.add_argument("--seconds", type=float, default=8.0)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    client = Go2WebRTCClient(
        Go2Config(
            ip=cfg.go2_ip,
            aes_128_key=cfg.go2_aes_128_key,
            webrtc_method=cfg.go2_webrtc_method,
            serial_number=cfg.go2_serial_number,
            remote_username=cfg.go2_remote_username,
            remote_password=cfg.go2_remote_password,
            remote_region=cfg.go2_remote_region,
            remote_device_type=cfg.go2_remote_device_type,
        )
    )
    frames = 0
    first_ts: float | None = None

    async def on_track(track: Any) -> None:
        nonlocal frames, first_ts
        while True:
            await track.recv()
            frames += 1
            first_ts = first_ts or time.monotonic()
            if frames in {1, 5, 15, 30}:
                print(f"video frame {frames}")

    try:
        await client.connect()
        conn = getattr(client, "_conn", None)
        video = getattr(conn, "video", None)
        if video is None:
            print("video interface not found")
            return 2
        video.switchVideoChannel(True)
        video.add_track_callback(on_track)
        await asyncio.sleep(max(1.0, args.seconds))
        print(f"video_frames={frames}")
        if frames <= 0:
            print("No video frames arrived. Try closing other viewers/phone apps and retest the same GO2_IP.")
            return 1
        print("video ok")
        return 0
    finally:
        await client.close()


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
