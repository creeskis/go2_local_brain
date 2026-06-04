"""Motion caps + a deadman timeout, shared by driver and brain.

These are still bounded in the driver, but they are no longer desk-test tiny.
Cooper verified WebRTC movement on hardware, so the normal prompt path can use
snappier walking, strafing, and turning while keeping short command windows.
"""

from __future__ import annotations

# Forward speed (m/s).
MAX_VX = 0.75
# Lateral / strafe speed (m/s).
MAX_VY = 0.40
# Yaw rate (rad/s).
MAX_VYAW = 1.10

# Default duration of a single move command, in seconds.
DEFAULT_MOVE_DURATION_S = 0.45

# Hard ceiling on duration. Stops a hallucinated `duration_s=600` from
# pinning the robot in a long move loop until the operator intervenes.
MAX_MOVE_DURATION_S = 2.0

# If no fresh command has arrived within this many seconds, the driver's
# deadman loop will publish zero velocity. Keep this short.
DEADMAN_TIMEOUT_S = 0.75


def clamp(value: float, lo: float, hi: float) -> float:
    """Clamp ``value`` into the inclusive range ``[lo, hi]``.

    Raises ValueError if ``lo > hi`` so misconfigured limits fail loudly.
    """
    if lo > hi:
        raise ValueError(f"clamp() got lo={lo} > hi={hi}")
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value
