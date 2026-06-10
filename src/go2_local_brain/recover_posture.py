"""One-shot posture recovery helper for a Go2 stuck in rest/lie-down state."""

from __future__ import annotations

import argparse
import asyncio
import logging

from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient


async def _amain() -> int:
    parser = argparse.ArgumentParser(description="Recover Go2 posture over WebRTC.")
    parser.add_argument("--mode", default="normal", help="Motion mode to request before standing.")
    parser.add_argument("--skip-recovery", action="store_true", help="Skip RecoveryStand and only use StandUp/BalanceStand.")
    parser.add_argument("--settle-s", type=float, default=2.0, help="Pause between posture commands.")
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
            force_motion_mode=args.mode,
        )
    )

    try:
        await client.connect()
        print(client.telemetry_report())
        if not args.skip_recovery:
            print("sending RecoveryStand")
            await client.recovery_stand()
            await asyncio.sleep(max(0.0, args.settle_s))
        print("sending StandUp")
        await client.stand_up()
        await asyncio.sleep(max(0.0, args.settle_s))
        print("sending BalanceStand")
        await client.balance_stand()
        await asyncio.sleep(0.5)
        print(client.telemetry_report())
        print("posture recovery sequence complete")
        return 0
    finally:
        await client.close()


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
