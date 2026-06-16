"""SSH-jump relay for a USB trigger attached to the Jetson."""

from __future__ import annotations

import asyncio
import os
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GunRelayConfig:
    dog_host: str = "192.168.123.121"
    dog_user: str = "root"
    jetson_host: str = "10.42.0.1"
    jetson_user: str = "root"
    command: str = "cat /dev/ttyUSB0 | xxd"
    connect_timeout_s: int = 4
    control_persist_s: int = 60


class GunRelay:
    """Start/stop the remote USB trigger command through the dog SSH jump."""

    def __init__(self, cfg: GunRelayConfig) -> None:
        self._cfg = cfg
        self._proc: asyncio.subprocess.Process | None = None
        self._master_proc: asyncio.subprocess.Process | None = None
        self._lock = asyncio.Lock()
        socket_name = f"go2-gun-{cfg.dog_host.replace('.', '_')}-{cfg.jetson_host.replace('.', '_')}.sock"
        self._control_path = str(Path(tempfile.gettempdir()) / socket_name)

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def preconnect(self) -> str:
        """Open an SSH control connection so later fire commands start faster."""
        if self._master_proc is not None and self._master_proc.returncode is None:
            return "ssh control socket already warming"
        args = self._ssh_base_args(control_master=True) + ["-N", self._target()]
        self._master_proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _stdout, stderr = await asyncio.wait_for(self._master_proc.communicate(), timeout=1.5)
        except asyncio.TimeoutError:
            return "ssh control socket warming"
        if self._master_proc.returncode == 0:
            return "ssh control socket ready"
        detail = stderr.decode(errors="replace").strip()
        return f"ssh preconnect exited: {detail or self._master_proc.returncode}"

    async def fire(self) -> str:
        async with self._lock:
            if self.active:
                return "fire already active"
            args = self._ssh_base_args(control_master=False) + ["-tt", self._target(), self._cfg.command]
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.sleep(0.15)
            if self._proc.returncode is not None:
                stderr = b""
                if self._proc.stderr is not None:
                    stderr = await self._proc.stderr.read()
                self._proc = None
                detail = stderr.decode(errors="replace").strip()
                raise RuntimeError(f"fire command exited immediately: {detail or 'no stderr'}")
            return "fire active"

    async def stop(self) -> str:
        async with self._lock:
            proc = self._proc
            self._proc = None
            if proc is None:
                return "fire already stopped"
            if proc.returncode is None:
                if proc.stdin is not None:
                    try:
                        proc.stdin.write(b"\x03")
                        await proc.stdin.drain()
                    except Exception:
                        pass
                await asyncio.sleep(0.25)
            if proc.returncode is None:
                try:
                    if os.name == "posix":
                        os.kill(proc.pid, signal.SIGINT)
                    else:
                        proc.terminate()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
            return "fire stopped"

    async def close(self) -> None:
        await self.stop()
        proc = self._master_proc
        self._master_proc = None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()

    def _target(self) -> str:
        return f"{self._cfg.jetson_user}@{self._cfg.jetson_host}"

    def _ssh_base_args(self, *, control_master: bool) -> list[str]:
        args = [
            "ssh",
            "-o",
            f"ConnectTimeout={self._cfg.connect_timeout_s}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"ProxyJump={self._cfg.dog_user}@{self._cfg.dog_host}",
            "-o",
            f"ControlPath={self._control_path}",
        ]
        if control_master:
            args.extend(["-o", "ControlMaster=auto", "-o", f"ControlPersist={self._cfg.control_persist_s}s"])
        else:
            args.extend(["-o", "ControlMaster=auto"])
        return args


def gun_relay_config_from_env() -> GunRelayConfig:
    return GunRelayConfig(
        dog_host=os.getenv("GUN_DOG_HOST", "192.168.123.121").strip() or "192.168.123.121",
        dog_user=os.getenv("GUN_DOG_USER", "root").strip() or "root",
        jetson_host=os.getenv("GUN_JETSON_HOST", "10.42.0.1").strip() or "10.42.0.1",
        jetson_user=os.getenv("GUN_JETSON_USER", "root").strip() or "root",
        command=os.getenv("GUN_FIRE_COMMAND", "cat /dev/ttyUSB0 | xxd").strip() or "cat /dev/ttyUSB0 | xxd",
        connect_timeout_s=max(1, int(os.getenv("GUN_SSH_CONNECT_TIMEOUT_S", "4"))),
        control_persist_s=max(5, int(os.getenv("GUN_SSH_CONTROL_PERSIST_S", "60"))),
    )
