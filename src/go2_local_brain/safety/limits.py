"""Motion caps + a deadman timeout, shared by driver and brain.

These are still bounded in the driver, but they are no longer desk-test tiny.
WebRTC movement has been verified on hardware, so the normal prompt path can use
snappier walking, strafing, and turning while keeping short command windows.
"""

from __future__ import annotations

# Forward speed (m/s) - Bumped from 0.75 for a faster stride
MAX_VX = 1.20

# Lateral / strafe speed (m/s) - Bumped from 0.40
MAX_VY = 0.65

# Yaw rate (rad/s) - Bumped from 1.10 for snappier turns
MAX_VYAW = 1.60

# Default duration of a single move command, in seconds - Raised from 0.45
DEFAULT_MOVE_DURATION_S = 1.00

# Hard ceiling on duration - Raised from 2.0 to allow longer continuous paths.
# Stops a hallucinated duration_s=600 from pinning the robot.
MAX_MOVE_DURATION_S = 5.0

# If no fresh command has arrived within this many seconds, the driver's
# deadman loop will publish zero velocity. Kept relatively short for safety.
DEADMAN_TIMEOUT_S = 0.85


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
