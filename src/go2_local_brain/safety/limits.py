"""Conservative motion caps + a deadman timeout, shared by driver and brain.

These are *intentionally* small. The Go2 can move much faster, but a desk-test
robot should not. Tune up only after you have verified everything works.
"""

from __future__ import annotations

# Forward speed (m/s).
MAX_VX = 0.35
# Lateral / strafe speed (m/s).
MAX_VY = 0.20
# Yaw rate (rad/s).
MAX_VYAW = 0.45

# Default duration of a single move command, in seconds.
DEFAULT_MOVE_DURATION_S = 0.35

# Hard ceiling on duration. Stops a hallucinated `duration_s=600` from
# pinning the robot in a long move loop until the operator intervenes.
MAX_MOVE_DURATION_S = 1.0

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
