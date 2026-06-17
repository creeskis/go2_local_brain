"""Script-backed USB trigger relay."""

from __future__ import annotations

import asyncio
import os
import signal
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GunRelayConfig:
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
    fire_command: str = "sudo bash -lc 'cat /dev/ttyUSB0 | xxd'"
    stop_command: str = "sudo bash -lc 'printf \"\\x30\" > /dev/ttyUSB0'"


class GunRelay:
    """Run the operator-provided fire/stop scripts from the cockpit."""

    def __init__(self, cfg: GunRelayConfig) -> None:
        self._cfg = cfg
        self._repo_root = Path(__file__).resolve().parents[2]
        self._proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def preconnect(self) -> str:
        return "script mode: no SSH warmup"

    async def test(self) -> str:
        return await self._run_to_completion(self._cfg.test_script)

    async def fire(self) -> str:
        async with self._lock:
            if self.active:
                return "fire already active"
            script = self._resolve_script(self._cfg.fire_script)
            self._proc = await asyncio.create_subprocess_exec(
                str(script),
                cwd=str(self._repo_root),
                env=self._env(),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=(os.name == "posix"),
            )
            await asyncio.sleep(0.35)
            if self._proc.returncode is not None:
                stderr = b""
                if self._proc.stderr is not None:
                    stderr = await self._proc.stderr.read()
                self._proc = None
                detail = stderr.decode(errors="replace").strip()
                raise RuntimeError(f"fire script exited immediately: {detail or 'no stderr'}")
            return f"fire script active: {script}"

    async def stop(self) -> str:
        async with self._lock:
            messages: list[str] = []
            proc = self._proc
            self._proc = None
            if proc is not None and proc.returncode is None:
                await self._terminate_fire_process(proc)
                messages.append("fire script stopped")
            else:
                messages.append("fire already stopped")
            messages.append(await self._run_to_completion(self._cfg.stop_script))
            return "; ".join(messages)

    async def close(self) -> None:
        await self.stop()

    async def _terminate_fire_process(self, proc: asyncio.subprocess.Process) -> None:
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGINT)
            else:
                proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()

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
        fire_command=os.getenv("GUN_FIRE_COMMAND", "sudo bash -lc 'cat /dev/ttyUSB0 | xxd'").strip()
        or "sudo bash -lc 'cat /dev/ttyUSB0 | xxd'",
        stop_command=os.getenv("GUN_STOP_COMMAND", "sudo bash -lc 'printf \"\\x30\" > /dev/ttyUSB0'").strip()
        or "sudo bash -lc 'printf \"\\x30\" > /dev/ttyUSB0'",
    )
