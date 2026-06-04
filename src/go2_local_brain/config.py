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
    force_motion_mode: str | None
    enable_exploration: bool
    exploration_min_obstacle_m: float
    exploration_mode: str
    exploration_max_duration_s: float


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


def _env_choice(name: str, default: str, choices: set[str]) -> str:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw if raw in choices else default


def load_config() -> AppConfig:
    """Read .env (if present) plus the process environment and return config."""
    load_dotenv(override=False)

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
        exploration_min_obstacle_m=max(0.05, _env_float("EXPLORATION_MIN_OBSTACLE_M", 0.35)),
        exploration_mode=_env_choice(
            "EXPLORATION_MODE",
            default="telemetry",
            choices={"telemetry", "relaxed", "blind"},
        ),
        exploration_max_duration_s=max(1.0, _env_float("EXPLORATION_MAX_DURATION_S", 15.0)),
    )
