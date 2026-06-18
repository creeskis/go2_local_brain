"""Persistent dog SSH tunnel for the Jetson USB trigger."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Any


@dataclass(frozen=True)
class GunRelayConfig:
    tunnel_script: str = "scripts/gun_tunnel_manual.sh"
    command_script: str = "scripts/gun_command_manual.sh"
    dog_host: str = "192.168.123.121"
    dog_user: str = "root"
    dog_password: str | None = None
    jetson_host: str = "10.42.0.2"
    jetson_user: str = "unitree"
    jetson_password: str | None = None
    jetson_sudo_password: str | None = None
    local_ssh_port: int = 10022
    log_file: str = "/tmp/go2_gun_relay.log"
    remote_log_file: str = "/tmp/go2_gun_remote.log"
    fire_command: str = "cat /dev/ttyUSB0 | xxd"
    stop_command: str = "printf '\\x30' > /dev/ttyUSB0"


class GunRelay:
    """Keep a dog SSH tunnel open and run short Jetson trigger commands."""

    def __init__(self, cfg: GunRelayConfig) -> None:
        self._cfg = cfg
        self._repo_root = Path(__file__).resolve().parents[2]
        self._tunnel: asyncio.subprocess.Process | None = None
        self._active = False
        self._state = "idle"
        self._last_result = ""
        self._last_error = ""
        self._last_action_ts = 0.0
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._active

    @property
    def state(self) -> str:
        return self._state

    async def preconnect(self) -> str:
        try:
            await self._ensure_tunnel()
            return self._remember("ready", "gun SSH tunnel ready")
        except Exception as exc:
            self._remember_error("error", str(exc))
            raise

    async def test(self) -> str:
        async with self._lock:
            self._state = "checking"
            try:
                await self._ensure_tunnel_locked()
                result = await self._run_command("TEST")
            except Exception as exc:
                self._remember_error("error", str(exc))
                raise
            return self._remember("ready" if not self._active else "firing", result)

    async def status(self) -> str:
        async with self._lock:
            self._state = "checking"
            try:
                await self._ensure_tunnel_locked()
                result = await self._run_command("STATUS")
            except Exception as exc:
                self._remember_error("error", str(exc))
                raise
            self._active = "active=1" in result
            return self._remember("firing" if self._active else "ready", result)

    async def fire(self) -> str:
        async with self._lock:
            if self._active:
                return self._remember("firing", self._last_result or "gun already firing")
            self._state = "starting"
            try:
                await self._ensure_tunnel_locked()
                result = await self._run_command("START")
            except Exception as exc:
                self._active = False
                self._remember_error("error", str(exc))
                raise
            self._active = True
            return self._remember("firing", result)

    async def stop(self) -> str:
        async with self._lock:
            self._state = "stopping"
            try:
                await self._ensure_tunnel_locked()
                result = await self._run_command("STOP")
            except Exception as exc:
                self._remember_error("error", str(exc))
                raise
            self._active = False
            return self._remember("ready", result)

    async def close(self) -> None:
        async with self._lock:
            proc = self._tunnel
            self._active = False
            self._state = "idle"
            if proc is None or proc.returncode is not None:
                self._tunnel = None
                return
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            self._tunnel = None

    def snapshot(self) -> dict[str, Any]:
        tunnel_alive = self._tunnel is not None and self._tunnel.returncode is None
        return {
            "active": self._active,
            "state": self._state,
            "tunnel_alive": tunnel_alive,
            "last_result": self._last_result,
            "last_error": self._last_error,
            "last_action_ts": self._last_action_ts,
            "log_file": self._cfg.log_file,
            "remote_log_file": self._cfg.remote_log_file,
            "log_tail": self._tail_log(self._cfg.log_file),
        }

    async def _ensure_tunnel(self) -> None:
        async with self._lock:
            await self._ensure_tunnel_locked()

    async def _ensure_tunnel_locked(self) -> None:
        if self._tunnel is not None and self._tunnel.returncode is None:
            return
        script = self._resolve_script(self._cfg.tunnel_script)
        self._tunnel = await asyncio.create_subprocess_exec(
            str(script),
            cwd=str(self._repo_root),
            env=self._env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
        try:
            await self._read_tunnel_ready(timeout=20.0)
        except Exception:
            detail = await self._tunnel_error()
            raise RuntimeError(f"gun SSH tunnel failed to start: {detail}") from None

    async def _read_tunnel_ready(self, *, timeout: float) -> str:
        proc = self._tunnel
        if proc is None or proc.stdout is None:
            raise RuntimeError("gun SSH tunnel is not open")
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                detail = await self._tunnel_error()
                raise RuntimeError(f"timeout waiting for tunnel: {detail}") from None
            if not raw:
                detail = await self._tunnel_error()
                raise RuntimeError(f"gun SSH tunnel exited while starting: {detail}")
            line = raw.decode(errors="replace").strip()
            if line.startswith("ERR "):
                raise RuntimeError(line[4:])
            if line.startswith("READY"):
                return line

    async def _run_command(self, action: str) -> str:
        script = self._resolve_script(self._cfg.command_script)
        env = self._env()
        env["GUN_ACTION"] = action
        proc = await asyncio.create_subprocess_exec(
            str(script),
            cwd=str(self._repo_root),
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        if proc.returncode != 0:
            detail = "; ".join(part for part in (err, out, f"exit {proc.returncode}") if part)
            raise RuntimeError(detail)
        return out or f"OK {action}"

    def _remember(self, state: str, result: str) -> str:
        self._state = state
        self._last_result = result
        self._last_error = ""
        self._last_action_ts = time.time()
        return result

    def _remember_error(self, state: str, error: str) -> None:
        self._state = state
        self._last_error = error
        self._last_result = error
        self._last_action_ts = time.time()

    def _tail_log(self, log_file: str, *, max_lines: int = 12) -> list[str]:
        try:
            path = Path(log_file).expanduser()
            if not path.exists() or not path.is_file():
                return []
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()[-max_lines:]
            return [line.rstrip() for line in lines]
        except OSError as exc:
            return [f"log unavailable: {exc}"]

    async def _tunnel_error(self) -> str:
        proc = self._tunnel
        if proc is None or proc.stderr is None:
            return "no stderr"
        if proc.returncode is None:
            return "tunnel still running"
        stderr = await proc.stderr.read()
        return stderr.decode(errors="replace").strip() or f"exit {proc.returncode}"

    async def _run_to_completion(self, script_name: str) -> str:
        script = self._resolve_script(script_name)
        proc = await asyncio.create_subprocess_exec(
            str(script),
            cwd=str(self._repo_root),
            env=self._env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        out = stdout.decode(errors="replace").strip()
        err = stderr.decode(errors="replace").strip()
        if proc.returncode != 0:
            raise RuntimeError(err or out or f"{script} exited {proc.returncode}")
        return out or f"{script.name} ok"

    def _resolve_script(self, script_name: str) -> Path:
        script = Path(script_name).expanduser()
        if not script.is_absolute():
            script = self._repo_root / script
        if not script.exists():
            raise RuntimeError(f"gun script not found: {script}")
        return script

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update(
            {
                "GUN_DOG_HOST": self._cfg.dog_host,
                "GUN_DOG_USER": self._cfg.dog_user,
                "GUN_JETSON_HOST": self._cfg.jetson_host,
                "GUN_JETSON_USER": self._cfg.jetson_user,
                "GUN_LOCAL_SSH_PORT": str(self._cfg.local_ssh_port),
                "GUN_LOG_FILE": self._cfg.log_file,
                "GUN_REMOTE_LOG_FILE": self._cfg.remote_log_file,
                "GUN_FIRE_COMMAND": self._cfg.fire_command,
                "GUN_STOP_COMMAND": self._cfg.stop_command,
            }
        )
        if self._cfg.dog_password:
            env["GUN_DOG_PASSWORD"] = self._cfg.dog_password
        if self._cfg.jetson_password:
            env["GUN_JETSON_PASSWORD"] = self._cfg.jetson_password
        if self._cfg.jetson_sudo_password:
            env["GUN_JETSON_SUDO_PASSWORD"] = self._cfg.jetson_sudo_password
        return env


def gun_relay_config_from_env() -> GunRelayConfig:
    return GunRelayConfig(
        tunnel_script=os.getenv("GUN_TUNNEL_SCRIPT", "scripts/gun_tunnel_manual.sh").strip()
        or "scripts/gun_tunnel_manual.sh",
        command_script=os.getenv("GUN_COMMAND_SCRIPT", "scripts/gun_command_manual.sh").strip()
        or "scripts/gun_command_manual.sh",
        dog_host=os.getenv("GUN_DOG_HOST", "192.168.123.121").strip() or "192.168.123.121",
        dog_user=os.getenv("GUN_DOG_USER", "root").strip() or "root",
        dog_password=os.getenv("GUN_DOG_PASSWORD", "").strip() or None,
        jetson_host=os.getenv("GUN_JETSON_HOST", "10.42.0.2").strip() or "10.42.0.2",
        jetson_user=os.getenv("GUN_JETSON_USER", "unitree").strip() or "unitree",
        jetson_password=os.getenv("GUN_JETSON_PASSWORD", "").strip() or None,
        jetson_sudo_password=os.getenv("GUN_JETSON_SUDO_PASSWORD", "").strip()
        or os.getenv("GUN_JETSON_PASSWORD", "").strip()
        or None,
        local_ssh_port=max(1, int(os.getenv("GUN_LOCAL_SSH_PORT", "10022"))),
        log_file=os.getenv("GUN_LOG_FILE", "/tmp/go2_gun_relay.log").strip() or "/tmp/go2_gun_relay.log",
        remote_log_file=os.getenv("GUN_REMOTE_LOG_FILE", "/tmp/go2_gun_remote.log").strip()
        or "/tmp/go2_gun_remote.log",
        fire_command=os.getenv("GUN_FIRE_COMMAND", "cat /dev/ttyUSB0 | xxd").strip()
        or "cat /dev/ttyUSB0 | xxd",
        stop_command=os.getenv("GUN_STOP_COMMAND", "printf '\\x30' > /dev/ttyUSB0").strip()
        or "printf '\\x30' > /dev/ttyUSB0",
    )
