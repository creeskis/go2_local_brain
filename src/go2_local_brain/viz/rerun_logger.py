"""Placeholder Rerun logger.

This is intentionally tiny. We'll flesh it out once we have something
worth visualizing (camera frames, IMU, footstep plan, etc.).
"""

from __future__ import annotations

import rerun as rr


class RerunLogger:
    """Minimal wrapper so callers don't talk to ``rr`` directly yet."""

    def __init__(self, app_name: str = "go2_local_brain") -> None:
        self._app_name = app_name
        self._started = False

    def start(self) -> None:
        """Initialize a Rerun recording. Safe to call more than once."""
        if self._started:
            return
        rr.init(self._app_name, spawn=False)
        self._started = True

    def log_text(self, path: str, text: str) -> None:
        if not self._started:
            return
        rr.log(path, rr.TextLog(text))

    def log_scalar(self, path: str, value: float) -> None:
        if not self._started:
            return
        rr.log(path, rr.Scalar(float(value)))
