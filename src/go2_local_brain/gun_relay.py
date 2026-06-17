"""Script-backed USB trigger relay."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GunRelayConfig:
    session_script: str = "scripts/gun_session_manual.sh"
    fire_script: str = "scripts/gun_fire_manual.sh"
    stop_script: str = "scripts/gun_stop_manual.sh"
    test_script: str = "scripts/gun_test_manual.sh"
    dog_host: str = "192.168.123.121"
    dog_user: str = "root"
    dog_password: str | None = None
    jetson_host: str = "10.42.0.2"
    jetson_user: str = "unitree"
    jetson_password: str | None = None
    jetson_sudo_password: str | None = None
    fire_command: str = "cat /dev/ttyUSB0 | xxd"
    stop_command: str = "printf '\\x30' > /dev/ttyUSB0"


class GunRelay:
    """Run the operator-provided fire/stop scripts from the cockpit."""

    def __init__(self, cfg: GunRelayConfig) -> None:
        self._cfg = cfg
        self._repo_root = Path(__file__).resolve().parents[2]
        self._session: asyncio.subprocess.Process | None = None
        self._active = False
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._active

    async def preconnect(self) -> str:
        await self._ensure_session()
        return "gun SSH session ready"

    async def test(self) -> str:
        async with self._lock:
            await self._ensure_session_locked()
            return await self._send_session_command("TEST", "OK TEST")

    async def fire(self) -> str:
        async with self._lock:
            await self._ensure_session_locked()
            result = await self._send_session_command("START", "OK START")
            self._active = True
            return result

    async def stop(self) -> str:
        async with self._lock:
            await self._ensure_session_locked()
            result = await self._send_session_command("STOP", "OK STOP")
            self._active = False
            return result

    async def close(self) -> None:
        async with self._lock:
            proc = self._session
            self._active = False
            if proc is None or proc.returncode is not None:
                self._session = None
                return
            try:
                await self._send_session_command("EXIT", "OK EXIT")
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except Exception:  # noqa: BLE001
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            finally:
                self._session = None

    async def _ensure_session(self) -> None:
        async with self._lock:
            await self._ensure_session_locked()

    async def _ensure_session_locked(self) -> None:
        if self._session is not None and self._session.returncode is None:
            return
        script = self._resolve_script(self._cfg.session_script)
        self._session = await asyncio.create_subprocess_exec(
            str(script),
            cwd=str(self._repo_root),
            env=self._env(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )
        try:
            await self._read_until("READY", timeout=20.0)
        except Exception:
            detail = await self._session_error()
            raise RuntimeError(f"gun SSH session failed to start: {detail}") from None

    async def _send_session_command(self, command: str, expected: str) -> str:
        proc = self._session
        if proc is None or proc.stdin is None:
            raise RuntimeError("gun SSH session is not open")
        proc.stdin.write(f"{command}\n".encode())
        try:
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            detail = await self._session_error()
            self._session = None
            self._active = False
            raise RuntimeError(f"gun SSH session closed before {command}: {detail}") from None
        return await self._read_until(expected, timeout=20.0)

    async def _read_until(self, expected: str, *, timeout: float) -> str:
        proc = self._session
        if proc is None or proc.stdout is None:
            raise RuntimeError("gun SSH session is not open")
        lines: list[str] = []
        while True:
            try:
                raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError:
                detail = await self._session_error()
                raise RuntimeError(f"timeout waiting for {expected}: {detail}") from None
            if not raw:
                detail = await self._session_error()
                raise RuntimeError(f"gun SSH session exited while waiting for {expected}: {detail}")
            line = raw.decode(errors="replace").strip()
            if line:
                lines.append(line)
            if line.startswith("ERR "):
                raise RuntimeError(line[4:])
            if line.startswith(expected):
                return line

    async def _session_error(self) -> str:
        proc = self._session
        if proc is None or proc.stderr is None:
            return "no stderr"
        if proc.returncode is None:
            return "session still running"
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
        session_script=os.getenv("GUN_SESSION_SCRIPT", "scripts/gun_session_manual.sh").strip()
        or "scripts/gun_session_manual.sh",
        fire_script=os.getenv("GUN_FIRE_SCRIPT", "scripts/gun_fire_manual.sh").strip()
        or "scripts/gun_fire_manual.sh",
        stop_script=os.getenv("GUN_STOP_SCRIPT", "scripts/gun_stop_manual.sh").strip()
        or "scripts/gun_stop_manual.sh",
        test_script=os.getenv("GUN_TEST_SCRIPT", "scripts/gun_test_manual.sh").strip()
        or "scripts/gun_test_manual.sh",
        dog_host=os.getenv("GUN_DOG_HOST", "192.168.123.121").strip() or "192.168.123.121",
        dog_user=os.getenv("GUN_DOG_USER", "root").strip() or "root",
        dog_password=os.getenv("GUN_DOG_PASSWORD", "").strip() or None,
        jetson_host=os.getenv("GUN_JETSON_HOST", "10.42.0.2").strip() or "10.42.0.2",
        jetson_user=os.getenv("GUN_JETSON_USER", "unitree").strip() or "unitree",
        jetson_password=os.getenv("GUN_JETSON_PASSWORD", "").strip() or None,
        jetson_sudo_password=os.getenv("GUN_JETSON_SUDO_PASSWORD", "").strip()
        or os.getenv("GUN_JETSON_PASSWORD", "").strip()
        or None,
        fire_command=os.getenv("GUN_FIRE_COMMAND", "cat /dev/ttyUSB0 | xxd").strip()
        or "cat /dev/ttyUSB0 | xxd",
        stop_command=os.getenv("GUN_STOP_COMMAND", "printf '\\x30' > /dev/ttyUSB0").strip()
        or "printf '\\x30' > /dev/ttyUSB0",
    )
