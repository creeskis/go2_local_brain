"""Keyboard control resolution for the direct-control terminal.

Pure logic, no driver / no hardware: given the active mode, speed level, and
the set of currently-held keys (or a single key press), produce a
``ControlCommand`` that the GUI layer turns into a driver call. Fully
unit-testable.

Firmware reality (verified against unitree_webrtc_connect SPORT_CMD /
SPORT_CMD_MCF on Go2 firmware 1.1.7):

* ``Move`` (1008) is the ONLY continuous-velocity command, and it is valid
  in BalanceStand (normal walking). There is no API to velocity-drive while
  in BackStand/HandStand, and jump/flip are discrete one-shot actions.
* Flips exist in all four directions (front/back/left/right) -> they map
  cleanly to W/S/A/D as discrete per-press actions.
* Jump exists forward-only (``FrontJump``); there is no left/right/back jump.
* BackStand / HandStand are static one-shot postures (MCF table), not gaits.

So the modes are honest about what the hardware can do:

* ``normal``    : W/S/A/D/Q/E -> continuous Move velocity (real driving).
* ``flip``      : W/S/A/D -> front/back/left/right flip, one per key press.
* ``jump``      : W -> forward jump (only direction the firmware supports).
* ``backstand`` : a static posture you toggle into; WASD does NOT drive it.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional

from ..safety.limits import MAX_VX, MAX_VY, MAX_VYAW


class ControlMode(str, Enum):
    NORMAL = "normal"
    FLIP = "flip"
    JUMP = "jump"
    BACKSTAND = "backstand"


class SpeedLevel(str, Enum):
    SLOW = "slow"
    NORMAL = "normal"
    FAST = "fast"


# Fraction of the safety-clamped maximums applied per speed level. Kept well
# under 1.0 so even "fast" stays inside the driver's hard clamps.
_SPEED_SCALE: dict[SpeedLevel, float] = {
    SpeedLevel.SLOW: 0.25,
    SpeedLevel.NORMAL: 0.5,
    SpeedLevel.FAST: 0.85,
}

# Cycle order for the speed toggle.
_SPEED_CYCLE = [SpeedLevel.SLOW, SpeedLevel.NORMAL, SpeedLevel.FAST]

# Per-direction flip action names. The driver resolves these against
# SPORT_CMD/SPORT_CMD_MCF (MCF first) via advanced_action().
_FLIP_BY_KEY = {
    "w": "front_flip",
    "s": "back_flip",
    "a": "left_flip",
    "d": "right_flip",
}


@dataclass(frozen=True)
class ControlCommand:
    """Resolved intent for one control tick or key press.

    kind:
      "velocity" -> drive at (vx, vy, vyaw) for duration_s
      "action"   -> run a discrete driver.advanced_action(action)
      "stop"     -> driver.stop()
      "noop"     -> do nothing (e.g. WASD in backstand mode)
    """

    kind: str
    vx: float = 0.0
    vy: float = 0.0
    vyaw: float = 0.0
    duration_s: float = 0.0
    action: Optional[str] = None
    note: str = ""


def speed_scale(level: SpeedLevel) -> float:
    return _SPEED_SCALE[level]


def next_speed(level: SpeedLevel) -> SpeedLevel:
    """Cycle slow -> normal -> fast -> slow."""
    idx = _SPEED_CYCLE.index(level)
    return _SPEED_CYCLE[(idx + 1) % len(_SPEED_CYCLE)]


def resolve_held(mode: ControlMode, speed: SpeedLevel, keys: set[str], *, tick_s: float = 0.25) -> ControlCommand:
    """Resolve the continuous (held-keys) path. Only meaningful in NORMAL mode.

    keys: lowercase {'w','a','s','d','q','e'} currently held.
    In non-NORMAL modes this returns a noop/stop — those modes are driven by
    discrete key presses (resolve_press), not held velocity.
    """
    keys = {k.lower() for k in keys}

    if mode is not ControlMode.NORMAL:
        # Discrete-action modes don't velocity-drive. Hold position.
        return ControlCommand("noop", note=f"{mode.value} mode is press-driven, not held")

    scale = _SPEED_SCALE[speed]
    vx = vy = vyaw = 0.0
    if "w" in keys:
        vx += MAX_VX * scale
    if "s" in keys:
        vx -= MAX_VX * scale
    if "a" in keys:
        vy += MAX_VY * scale
    if "d" in keys:
        vy -= MAX_VY * scale
    if "q" in keys:
        vyaw += MAX_VYAW * scale
    if "e" in keys:
        vyaw -= MAX_VYAW * scale

    if vx == 0.0 and vy == 0.0 and vyaw == 0.0:
        return ControlCommand("stop", note="no keys held")
    return ControlCommand("velocity", vx=vx, vy=vy, vyaw=vyaw, duration_s=tick_s)


def resolve_press(mode: ControlMode, key: str) -> ControlCommand:
    """Resolve a single key DOWN event for the discrete-action modes.

    NORMAL mode is handled by resolve_held; here a press in NORMAL is a noop.
    """
    k = key.lower()

    if k == " " or k == "space":
        return ControlCommand("stop", note="space -> stop")

    if mode is ControlMode.FLIP:
        action = _FLIP_BY_KEY.get(k)
        if action is None:
            return ControlCommand("noop", note=f"{k!r} has no flip mapping")
        return ControlCommand("action", action=action, note=f"flip: {action}")

    if mode is ControlMode.JUMP:
        # Firmware exposes forward jump only.
        if k == "w":
            return ControlCommand("action", action="jump", note="forward jump")
        return ControlCommand("noop", note="only forward jump (W) is supported by firmware")

    if mode is ControlMode.BACKSTAND:
        # Static posture; WASD cannot drive it.
        return ControlCommand("noop", note="backstand is a static posture; WASD disabled")

    # NORMAL mode: presses are folded into the held-velocity path.
    return ControlCommand("noop", note="normal mode uses held-key velocity")


def mode_enter_action(mode: ControlMode) -> Optional[str]:
    """The driver action (if any) to run when ENTERING a mode.

    normal/jump/flip -> balance stand (so the dog is upright + ready).
    backstand        -> backstand posture.
    Returns an advanced_action name or None.
    """
    if mode is ControlMode.BACKSTAND:
        return "backstand"
    return "balance_stand"
