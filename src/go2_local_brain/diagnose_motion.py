"""Diagnose Go2 sport-command delivery after WebRTC connects."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from typing import Any

from .config import load_config
from .driver.webrtc_client import Go2Config, Go2WebRTCClient


def _pretty(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, sort_keys=True, default=str)
    except TypeError:
        return repr(value)


async def _try(label: str, coro: Any) -> Any:
    print(f"\n== {label} ==")
    try:
        result = await coro
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}")
        return None
    print(_pretty(result))
    return result


async def _amain() -> int:
    parser = argparse.ArgumentParser(description="Diagnose motion mode and sport command responses.")
    parser.add_argument("--mode", default="normal", help="Motion mode to request before posture commands.")
    parser.add_argument("--move-test", action="store_true", help="Send a tiny forward movement after standing.")
    parser.add_argument("--no-stand", action="store_true", help="Do not send posture commands; only query state.")
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

    try:
        await client.connect()
        print("\n== telemetry before ==")
        print(client.telemetry_report())
        print("\n== available posture commands ==")
        commands = client.available_sport_commands()
        for name in ("RecoveryStand", "RecoveryStandUp", "StandUp", "BalanceStand", "StandDown", "Sit", "Move", "StopMove"):
            print(f"{name}: {'yes' if name in commands else 'no'}")

        await _try("motion mode status before", client.motion_mode_status())
        await _try(f"set motion mode {args.mode!r}", client.set_motion_mode(args.mode))
        await _try("motion mode status after set", client.motion_mode_status())

        if not args.no_stand:
            await _try("RecoveryStand", client.sport_command_response("RecoveryStand"))
            await asyncio.sleep(2.0)
            await _try("StandUp", client.sport_command_response("StandUp"))
            await asyncio.sleep(3.0)
            await _try("BalanceStand", client.sport_command_response("BalanceStand"))
            await asyncio.sleep(1.0)

        if args.move_test:
            print("\n== tiny move test ==")
            await client.move(0.12, 0.0, 0.0, 0.35)
            await asyncio.sleep(0.5)
            await client.stop()
            print("tiny move command sent")

        print("\n== telemetry after ==")
        print(client.telemetry_report())
        return 0
    finally:
        await client.close()


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
