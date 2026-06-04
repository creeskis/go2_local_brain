"""Application configuration loaded from environment / .env file."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    """Static config the rest of the app reads at startup."""

    go2_ip: str
    go2_aes_128_key: str | None
    ollama_model: str
    # Optional motion-mode override. If set (e.g. "normal" or "mcf"), the
    # driver will switch the robot into that mode during connect(). Leave
    # None to skip - Cooper's custom 1.1.7 package may already pick the
    # right mode and we don't want to fight it by default.
    force_motion_mode: str | None
    # Exploration is off by default. It must be explicitly enabled because it
    # can initiate multiple autonomous move/turn steps from one prompt.
    enable_exploration: bool
    exploration_min_obstacle_m: float


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def load_config() -> AppConfig:
    """Read .env (if present) plus the process environment and return an AppConfig.

    .env is loaded best-effort: real env vars always win, so it's safe to run
    in shells where the operator has already exported overrides.
    """
    load_dotenv(override=False)

    # Default matches Cooper's live Go2 STA IP. Adjust in .env if your
    # robot is on a different address.
    go2_ip = os.getenv("GO2_IP", "192.168.123.121").strip()
    raw_key = os.getenv("GO2_AES_128_KEY", "").strip()
    aes_key = raw_key if raw_key else None
    ollama_model = os.getenv("OLLAMA_MODEL", "qwen3:1.7b").strip()
    raw_mode = os.getenv("FORCE_MOTION_MODE", "").strip()
    force_mode = raw_mode if raw_mode else None

    return AppConfig(
        go2_ip=go2_ip,
        go2_aes_128_key=aes_key,
        ollama_model=ollama_model,
        force_motion_mode=force_mode,
        enable_exploration=_env_bool("ENABLE_EXPLORATION", default=False),
        exploration_min_obstacle_m=max(0.1, _env_float("EXPLORATION_MIN_OBSTACLE_M", 0.35)),
    )
