"""Motion caps + a deadman timeout, shared by driver and brain.

These are still bounded in the driver, but they are no longer desk-test tiny.
WebRTC movement has been verified on hardware, so the normal prompt path can use
snappier walking, strafing, and turning while keeping short command windows.
"""

from __future__ import annotations

# Forward speed (m/s). Operator mode: allow the full fast-walk range tested
# through the localhost cockpit while keeping the deadman stop active.
MAX_VX = 2.00

# Lateral / strafe speed (m/s).
MAX_VY = 1.00

# Yaw rate (rad/s).
MAX_VYAW = 2.50

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
