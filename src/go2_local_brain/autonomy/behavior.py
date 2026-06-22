"""Pure decision logic for headless roam + follow autonomy.

Two small, fully unit-testable helpers used by ``autonomy_agent``:

* :func:`select_mode` -- follow a visible person, briefly scan after losing them,
  then fall back to LiDAR roam. This encodes "follow people until they're out of
  frame, then roam freely".
* :func:`gate_follow_with_lidar` -- never let the follow controller drive the
  robot forward into an obstacle the LiDAR can already see.

No hardware, no I/O, so the agent's behaviour is testable without a robot.
"""

from __future__ import annotations

from dataclasses import dataclass

from .lidar_map import LidarObstacleSummary

MODE_FOLLOW = "follow"
MODE_SCAN = "scan"
MODE_ROAM = "roam"

_OPEN_M = 99.0


def select_mode(person_visible: bool, seconds_since_person: float, *, follow_grace_s: float) -> str:
    """Pick the active behaviour.

    * person in view            -> FOLLOW
    * lost within the grace gap  -> SCAN (turn in place to re-find them)
    * lost longer than the grace -> ROAM (LiDAR wander)
    """
    if person_visible:
        return MODE_FOLLOW
    if seconds_since_person <= follow_grace_s:
        return MODE_SCAN
    return MODE_ROAM


@dataclass(frozen=True)
class GatedMove:
    vx: float
    vy: float
    vyaw: float
    duration_s: float
    note: str


def gate_follow_with_lidar(
    vx: float,
    vyaw: float,
    duration_s: float,
    summary: LidarObstacleSummary,
    *,
    stop_distance_m: float,
    turn_rate_rps: float,
) -> GatedMove:
    """Clamp a follow command so the robot never chases a person into a wall.

    Only *forward* motion is gated: if LiDAR is fresh and the front sector is
    closer than ``stop_distance_m`` we drop the forward component and turn toward
    the more open side instead. Backing up / turning are passed through.
    """
    front = summary.front_m
    if vx > 0.0 and summary.fresh and front is not None and front < stop_distance_m:
        left = summary.left_m if summary.left_m is not None else _OPEN_M
        right = summary.right_m if summary.right_m is not None else _OPEN_M
        turn = turn_rate_rps if left >= right else -turn_rate_rps
        side = "left" if turn >= 0 else "right"
        return GatedMove(0.0, 0.0, turn, duration_s, f"follow blocked: front {front:.2f}m, veer {side}")
    return GatedMove(vx, 0.0, vyaw, duration_s, "follow")
