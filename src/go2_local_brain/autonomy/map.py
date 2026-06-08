"""Simple JSON map format for first-pass patrol autonomy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Waypoint:
    """Approximate named map target."""

    name: str
    x: float
    y: float
    yaw: float = 0.0
    note: str = ""


@dataclass(frozen=True)
class PatrolMap:
    """Loaded patrol map with a named route."""

    name: str
    waypoints: dict[str, Waypoint]
    patrol_route: list[str]
    no_go_zones: list[str]

    def next_waypoint(self, index: int) -> tuple[int, Waypoint]:
        if not self.patrol_route:
            raise ValueError("patrol_route is empty")
        route_index = index % len(self.patrol_route)
        name = self.patrol_route[route_index]
        try:
            return route_index, self.waypoints[name]
        except KeyError as exc:
            raise ValueError(f"route references unknown waypoint {name!r}") from exc


def load_patrol_map(path: str | Path) -> PatrolMap:
    """Load a map from JSON and validate the route."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("map root must be an object")

    waypoints_raw = raw.get("waypoints", {})
    if not isinstance(waypoints_raw, dict) or not waypoints_raw:
        raise ValueError("map must contain waypoints")

    waypoints: dict[str, Waypoint] = {}
    for name, data in waypoints_raw.items():
        if not isinstance(data, dict):
            raise ValueError(f"waypoint {name!r} must be an object")
        waypoints[str(name)] = _waypoint_from_dict(str(name), data)

    route = raw.get("patrol_route", [])
    if not isinstance(route, list) or not route:
        raise ValueError("map must contain a non-empty patrol_route")
    patrol_route = [str(name) for name in route]
    missing = [name for name in patrol_route if name not in waypoints]
    if missing:
        raise ValueError(f"patrol_route references missing waypoints: {missing}")

    zones = raw.get("no_go_zones", [])
    if not isinstance(zones, list):
        raise ValueError("no_go_zones must be a list")

    return PatrolMap(
        name=str(raw.get("name", Path(path).stem)),
        waypoints=waypoints,
        patrol_route=patrol_route,
        no_go_zones=[str(zone) for zone in zones],
    )


def _waypoint_from_dict(name: str, data: dict[str, Any]) -> Waypoint:
    try:
        x = float(data["x"])
        y = float(data["y"])
    except KeyError as exc:
        raise ValueError(f"waypoint {name!r} needs x and y") from exc
    return Waypoint(
        name=name,
        x=x,
        y=y,
        yaw=float(data.get("yaw", 0.0)),
        note=str(data.get("note", "")),
    )
