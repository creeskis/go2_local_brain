"""Minimal WebRTC handshake diagnostic for the Go2."""

from __future__ import annotations

import asyncio
import logging
import urllib.request

from .config import load_config
from .driver.webrtc_client import (
    Go2Config,
    _build_unitree_connection,
    _friendly_connect_error,
    _local_ip_for_target,
    _method_name,
    _resolve_webrtc_method,
)


async def _amain() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    try:
        import unitree_webrtc_connect  # type: ignore
        from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod  # type: ignore
    except Exception as exc:  # noqa: BLE001
        print(f"import failed: {type(exc).__name__}: {exc}")
        return 2

    print(f"unitree_webrtc_connect={getattr(unitree_webrtc_connect, '__file__', 'unknown')}")
    print(f"version={getattr(unitree_webrtc_connect, '__version__', 'unknown')}")
    print(f"target_ip={cfg.go2_ip}")
    print(f"method={cfg.go2_webrtc_method}")
    print(f"local_ip={_local_ip_for_target(cfg.go2_ip) or 'unknown'}")
    print(f"aes_key={'present' if cfg.go2_aes_128_key else 'blank'}")

    for url in (f"http://{cfg.go2_ip}:9991/con_notify", f"http://{cfg.go2_ip}:8081/con_notify"):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                data = response.read(120)
            print(f"{url}: ok first_bytes={data[:40]!r}")
            break
        except Exception as exc:  # noqa: BLE001
            print(f"{url}: {type(exc).__name__}: {exc}")

    method = _resolve_webrtc_method(WebRTCConnectionMethod, cfg.go2_webrtc_method)
    go2_cfg = Go2Config(
        ip=cfg.go2_ip,
        aes_128_key=cfg.go2_aes_128_key,
        webrtc_method=cfg.go2_webrtc_method,
        serial_number=cfg.go2_serial_number,
        remote_username=cfg.go2_remote_username,
        remote_password=cfg.go2_remote_password,
        remote_region=cfg.go2_remote_region,
        remote_device_type=cfg.go2_remote_device_type,
    )
    kwargs = {"connectionMethod": method}
    method_name = _method_name(method)
    if method_name != "LocalAP":
        kwargs["ip"] = cfg.go2_ip
    if cfg.go2_serial_number:
        kwargs["serialNumber"] = cfg.go2_serial_number
    if method_name == "Remote":
        if cfg.go2_remote_username:
            kwargs["username"] = cfg.go2_remote_username
        if cfg.go2_remote_password:
            kwargs["password"] = cfg.go2_remote_password
        kwargs["region"] = cfg.go2_remote_region
        kwargs["device_type"] = cfg.go2_remote_device_type

    conn = None
    try:
        conn = _build_unitree_connection(UnitreeWebRTCConnection, method, kwargs, cfg.go2_aes_128_key)
        print("connect: starting")
        await asyncio.wait_for(conn.connect(), timeout=25)
        print("connect: ok")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(_friendly_connect_error(exc, go2_cfg, method))
        return 1
    finally:
        if conn is not None:
            try:
                await conn.disconnect()
            except Exception:
                pass


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
