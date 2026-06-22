"""Headless LiDAR patrol planning: turn obstacle sectors into a drive command.

Pure logic, no hardware and no I/O so it is fully unit-testable. Given a
:class:`~go2_local_brain.autonomy.lidar_map.LidarObstacleSummary` (front / left /
right / rear clearances produced by ``LidarObstacleField`` from the live LiDAR
voxel cloud), decide how the robot should drive so it roams an area and avoids
obstacles. The async agent in ``patrol_agent.py`` feeds it real LiDAR and turns
each :class:`PatrolDecision` into a ``driver.move()`` call.

Design notes
------------
* Distances are robot-relative metres. ``None`` from a sector means "no returns
  in range" which we treat as *clear* (a large distance), not as blocked.
* The planner is stateless; :class:`PatrolController` adds the small amount of
  state needed to avoid oscillating between left/right and to escape when the
  robot keeps re-detecting the same obstacle (a corner).
* Outputs already respect the safety caps in ``safety.limits``; the driver
  clamps again, so a bug here can never exceed the hardware limits.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..safety.limits import MAX_VX, MAX_VYAW, clamp
from .lidar_map import LidarObstacleSummary

# A sector with no LiDAR returns is "wide open" for planning purposes.
_OPEN_M = 99.0


@dataclass(frozen=True)
class PatrolParams:
    """Tunable patrol behaviour. Defaults are conservative indoor values."""

    forward_speed_mps: float = 0.45      # cruise speed when the path is clear
    backup_speed_mps: float = 0.20       # reverse speed when a wall is too close
    turn_rate_rps: float = 0.85          # yaw rate while avoiding
    wander_yaw_rps: float = 0.12         # gentle bias so a clear run still sweeps

    stop_distance_m: float = 0.55        # front closer than this -> back up + turn
    slow_distance_m: float = 1.10        # front in this band -> creep + steer away

    forward_step_s: float = 0.50         # how long one cruise/creep command runs
    turn_step_s: float = 0.45            # how long one avoidance turn runs
    escape_turn_s: float = 0.95          # longer pivot used to break out of a corner

    stuck_pivots: int = 6                # consecutive avoids before an escape pivot
    stale_grace_s: float = 1.0           # tolerate brief LiDAR gaps before holding
    allow_blind: bool = False            # roam slowly even with no LiDAR (risky)

    def __post_init__(self) -> None:
        for name in ("forward_speed_mps", "backup_speed_mps"):
            value = getattr(self, name)
            if not 0.0 <= value <= MAX_VX:
                raise ValueError(f"{name}={value} must be in [0, {MAX_VX}]")
        if not 0.0 <= self.turn_rate_rps <= MAX_VYAW:
            raise ValueError(f"turn_rate_rps must be in [0, {MAX_VYAW}]")
        if self.slow_distance_m < self.stop_distance_m:
            raise ValueError("slow_distance_m must be >= stop_distance_m")
        if self.stuck_pivots < 1:
            raise ValueError("stuck_pivots must be >= 1")


@dataclass(frozen=True)
class PatrolDecision:
    """One drive command the agent will hand to ``driver.move()``."""

    vx: float
    vy: float
    vyaw: float
    duration_s: float
    action: str   # forward | steer | avoid | escape | blind | hold
    note: str

    @property
    def moves(self) -> bool:
        return self.action not in ("hold",)


def _sector(value: float | None) -> float:
    return _OPEN_M if value is None else value


def plan_patrol_step(
    summary: LidarObstacleSummary,
    params: PatrolParams,
    *,
    turn_bias: float = 1.0,
) -> PatrolDecision:
    """Decide a single patrol move from the latest LiDAR clearances.

    ``turn_bias`` (+1 prefer left, -1 prefer right) is only used when both sides
    look equally open, so the robot keeps sweeping the same way instead of
    jittering. Avoidance always turns toward the genuinely more open side.
    """
    front = _sector(summary.front_m)
    left = _sector(summary.left_m)
    right = _sector(summary.right_m)
    bias_sign = 1.0 if turn_bias >= 0 else -1.0

    # No usable LiDAR: hold position unless explicitly allowed to roam blind.
    if not summary.fresh or summary.point_count == 0:
        if not params.allow_blind:
            return PatrolDecision(0.0, 0.0, 0.0, params.turn_step_s, "hold", "no fresh LiDAR; holding")
        return PatrolDecision(
            params.backup_speed_mps, 0.0, params.wander_yaw_rps * bias_sign,
            params.forward_step_s, "blind", "no LiDAR; cautious blind roam",
        )

    # Pick the more open side; ties fall back to the sweep bias.
    if abs(left - right) < 0.10:
        open_sign = bias_sign
    else:
        open_sign = 1.0 if left > right else -1.0
    side_name = "left" if open_sign > 0 else "right"

    if front < params.stop_distance_m:
        return PatrolDecision(
            -params.backup_speed_mps, 0.0, params.turn_rate_rps * open_sign,
            params.turn_step_s, "avoid",
            f"front {front:.2f}m blocked; back up + turn {side_name}",
        )

    if front < params.slow_distance_m:
        return PatrolDecision(
            params.forward_speed_mps * 0.4, 0.0, params.turn_rate_rps * 0.55 * open_sign,
            params.forward_step_s, "steer",
            f"front {front:.2f}m close; creep + steer {side_name}",
        )

    # Clear ahead: cruise with a gentle wander so a long hallway still sweeps.
    return PatrolDecision(
        params.forward_speed_mps, 0.0, params.wander_yaw_rps * open_sign,
        params.forward_step_s, "forward",
        f"clear ({front:.2f}m); cruise",
    )


@dataclass
class PatrolController:
    """Stateful wrapper adding turn hysteresis and corner-escape behaviour."""

    params: PatrolParams
    _turn_bias: float = 1.0
    _avoid_streak: int = 0

    def step(self, summary: LidarObstacleSummary) -> PatrolDecision:
        decision = plan_patrol_step(summary, self.params, turn_bias=self._turn_bias)

        if decision.action in ("avoid", "steer"):
            # Lock the chosen turn direction so we don't ping-pong, and count
            # how long we've been stuck avoiding.
            self._turn_bias = 1.0 if decision.vyaw >= 0 else -1.0
            self._avoid_streak += 1
            if self._avoid_streak >= self.params.stuck_pivots:
                self._avoid_streak = 0
                vyaw = clamp(self.params.turn_rate_rps * self._turn_bias, -MAX_VYAW, MAX_VYAW)
                return PatrolDecision(
                    0.0, 0.0, vyaw, self.params.escape_turn_s, "escape",
                    "stuck avoiding; larger escape pivot",
                )
        else:
            self._avoid_streak = 0

        return decision
