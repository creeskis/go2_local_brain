"""Target-following helpers for AI autonomy mode."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Protocol

from .perception import Observation, best_human_detection


class FollowMover(Protocol):
    async def move(self, vx: float, vy: float = 0.0, vyaw: float = 0.0, duration_s: float = 0.0) -> None: ...


@dataclass(frozen=True)
class SoundCue:
    """A simple local-machine sound cue.

    A normal laptop microphone does not provide reliable direction. Direction is
    optional so future mic-array or stereo backends can provide it, while a mono
    backend can still trigger a scan.
    """

    timestamp: float
    level: float
    direction: float | None = None


@dataclass(frozen=True)
class FollowCommand:
    vx: float
    vyaw: float
    duration_s: float
    reason: str


@dataclass(frozen=True)
class FollowStatus:
    active: bool
    source: str
    last_action: str
    last_target: str


class HumanFollowController:
    """Convert the best human detection into short safe move windows."""

    def __init__(
        self,
        mover: FollowMover,
        *,
        target_height: float = 0.80,
        deadband: float = 0.12,
        max_forward: float = 1.15,
        max_turn: float = 0.55,
        duration_s: float = 0.45,
    ) -> None:
        self._mover = mover
        self._target_height = target_height
        self._deadband = deadband
        self._max_forward = max_forward
        self._max_turn = max_turn
        self._duration_s = duration_s
        self._last_action = "none"
        self._last_target = "none"
        self._last_command = FollowCommand(0.0, 0.0, 0.0, "none")
        self._smoothed_center_error: float | None = None
        self._smoothed_height: float | None = None

    @property
    def last_action(self) -> str:
        return self._last_action

    @property
    def last_target(self) -> str:
        return self._last_target

    @property
    def last_command(self) -> FollowCommand:
        return self._last_command

    async def step(self, observation: Observation, sound_cue: SoundCue | None = None) -> FollowCommand:
        command = self.plan(observation, sound_cue)
        await self._mover.move(command.vx, 0.0, command.vyaw, command.duration_s)
        self._last_action = command.reason
        self._last_command = command
        return command

    def plan(self, observation: Observation, sound_cue: SoundCue | None = None) -> FollowCommand:
        target = best_human_detection(observation)
        if target is None:
            self._smoothed_center_error = None
            self._smoothed_height = None
            if sound_cue is not None and time.time() - sound_cue.timestamp < 2.0:
                turn = 0.25 if sound_cue.direction is None else _clamp(sound_cue.direction, -self._max_turn, self._max_turn)
                self._last_target = "sound"
                command = FollowCommand(0.0, turn, self._duration_s, "scan toward sound")
                self._last_command = command
                return command
            self._last_target = "none"
            command = FollowCommand(0.0, 0.25, self._duration_s, "scan for person")
            self._last_command = command
            return command

        center_x = _relative_center(target.x, observation.frame_width)
        height = _relative_size(target.height, observation.frame_height)
        raw_center_error = 0.0 if center_x is None else center_x - 0.5
        center_error = _smooth(self._smoothed_center_error, raw_center_error, alpha=0.45)
        self._smoothed_center_error = center_error

        # Human on right means positive image error; Unitree yaw-right is negative here.
        turn = 0.0 if abs(center_error) < self._deadband else _clamp(-center_error * 1.4, -self._max_turn, self._max_turn)

        if height is None:
            forward = 0.0
        else:
            smoothed_height = _smooth(self._smoothed_height, height, alpha=0.45)
            self._smoothed_height = smoothed_height
            distance_error = self._target_height - smoothed_height
            forward = _clamp(distance_error * 2.2, -0.40, self._max_forward)
            if abs(distance_error) < 0.04:
                forward = 0.0

        self._last_target = f"person:{target.confidence:.2f}"
        if forward == 0.0 and turn == 0.0:
            reason = "hold person centered"
        elif forward > 0.0:
            reason = "follow person forward"
        elif forward < 0.0:
            reason = "back away from person"
        else:
            reason = "turn toward person"
        command = FollowCommand(forward, turn, self._duration_s, reason)
        self._last_command = command
        return command


class LocalSoundLevelProvider:
    """Optional mono microphone cue provider using sounddevice when installed."""

    def __init__(self, *, threshold: float = 0.08, sample_s: float = 0.08) -> None:
        self._threshold = threshold
        self._sample_s = sample_s
        self._last_error = ""

    @property
    def last_error(self) -> str:
        return self._last_error

    def listen_once(self) -> SoundCue | None:
        try:
            import numpy as np  # type: ignore
            import sounddevice as sd  # type: ignore
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"install audio deps for local sound cues: {exc}"
            return None

        try:
            sample_rate = 16000
            audio = sd.rec(int(sample_rate * self._sample_s), samplerate=sample_rate, channels=1, blocking=True)
            level = float(np.sqrt(np.mean(np.square(audio))))
        except Exception as exc:  # noqa: BLE001
            self._last_error = f"sound input unavailable: {exc}"
            return None

        self._last_error = ""
        if level < self._threshold:
            return None
        return SoundCue(timestamp=time.time(), level=level, direction=None)


def _relative_center(value: float | None, extent: int | None) -> float | None:
    if value is None:
        return None
    if value <= 1.0:
        return value
    if not extent:
        return None
    return value / extent


def _relative_size(value: float | None, extent: int | None) -> float | None:
    if value is None:
        return None
    if value <= 1.0:
        return value
    if not extent:
        return None
    return value / extent


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _smooth(previous: float | None, current: float, *, alpha: float) -> float:
    if previous is None:
        return current
    return previous * (1.0 - alpha) + current * alpha
