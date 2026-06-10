"""Simple JSON map format for first-pass patrol autonomy."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAP_SCHEMA_VERSION = 1


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
    metadata: dict[str, Any] = field(default_factory=dict)

    def next_waypoint(self, index: int) -> tuple[int, Waypoint]:
        if not self.patrol_route:
            raise ValueError("patrol_route is empty")
        route_index = index % len(self.patrol_route)
        name = self.patrol_route[route_index]
        try:
            return route_index, self.waypoints[name]
        except KeyError as exc:
            raise ValueError(f"route references unknown waypoint {name!r}") from exc

    def validate_for_patrol(self) -> None:
        if not self.waypoints:
            raise ValueError("map has no waypoints")
        if not self.patrol_route:
            raise ValueError("map has no patrol_route")
        missing = [name for name in self.patrol_route if name not in self.waypoints]
        if missing:
            raise ValueError(f"patrol_route references missing waypoints: {missing}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MAP_SCHEMA_VERSION,
            "name": self.name,
            "waypoints": {
                name: {"x": wp.x, "y": wp.y, "yaw": wp.yaw, "note": wp.note}
                for name, wp in sorted(self.waypoints.items())
            },
            "patrol_route": list(self.patrol_route),
            "no_go_zones": list(self.no_go_zones),
            "metadata": dict(self.metadata),
        }


def load_patrol_map(path: str | Path, *, require_route: bool = True) -> PatrolMap:
    """Load a map from JSON and validate the route."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    patrol_map = patrol_map_from_dict(raw, default_name=Path(path).stem)
    if require_route:
        patrol_map.validate_for_patrol()
    return patrol_map


def patrol_map_from_dict(raw: dict[str, Any], *, default_name: str = "untitled") -> PatrolMap:
    """Build a PatrolMap from already-parsed JSON data."""
    if not isinstance(raw, dict):
        raise ValueError("map root must be an object")

    waypoints_raw = raw.get("waypoints", {})
    if not isinstance(waypoints_raw, dict):
        raise ValueError("waypoints must be an object")

    waypoints: dict[str, Waypoint] = {}
    for name, data in waypoints_raw.items():
        if not isinstance(data, dict):
            raise ValueError(f"waypoint {name!r} must be an object")
        waypoints[str(name)] = _waypoint_from_dict(str(name), data)

    route = raw.get("patrol_route", [])
    if not isinstance(route, list):
        raise ValueError("patrol_route must be a list")
    patrol_route = [str(name) for name in route]

    zones = raw.get("no_go_zones", [])
    if not isinstance(zones, list):
        raise ValueError("no_go_zones must be a list")
    metadata = raw.get("metadata", {})
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be an object")

    return PatrolMap(
        name=str(raw.get("name", default_name)),
        waypoints=waypoints,
        patrol_route=patrol_route,
        no_go_zones=[str(zone) for zone in zones],
        metadata=dict(metadata),
    )


def empty_patrol_map(name: str = "untitled") -> PatrolMap:
    """Create a blank map draft that is not ready for autonomy yet."""
    return PatrolMap(name=name, waypoints={}, patrol_route=[], no_go_zones=[], metadata=_default_metadata())


def save_patrol_map(patrol_map: PatrolMap, maps_dir: str | Path) -> Path:
    """Save a map under a safe JSON filename and return the written path."""
    root = Path(maps_dir)
    root.mkdir(parents=True, exist_ok=True)
    filename = f"{safe_map_filename(patrol_map.name)}.json"
    path = root / filename
    payload = patrol_map.to_dict()
    metadata = dict(payload.get("metadata", {}))
    metadata.setdefault("created_ts", time.time())
    metadata["saved_ts"] = time.time()
    metadata.setdefault("coordinate_frame", "local_odometry_m")
    metadata.setdefault("localization_required", True)
    payload["metadata"] = metadata
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def list_patrol_maps(maps_dir: str | Path) -> list[dict[str, Any]]:
    """Return metadata for saved maps in a directory."""
    root = Path(maps_dir)
    if not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json")):
        try:
            patrol_map = load_patrol_map(path, require_route=False)
            ready = _map_ready(patrol_map)
            error = ""
        except Exception as exc:  # noqa: BLE001
            patrol_map = None
            ready = False
            error = str(exc)
        out.append(
            {
                "filename": path.name,
                "path": str(path),
                "name": patrol_map.name if patrol_map is not None else path.stem,
                "waypoint_count": len(patrol_map.waypoints) if patrol_map is not None else 0,
                "route_count": len(patrol_map.patrol_route) if patrol_map is not None else 0,
                "ready": ready,
                "error": error,
            }
        )
    return out


def safe_map_filename(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name.strip()).strip(".-").lower()
    return slug or "untitled"


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


def _map_ready(patrol_map: PatrolMap) -> bool:
    try:
        patrol_map.validate_for_patrol()
    except ValueError:
        return False
    return True


def _default_metadata() -> dict[str, Any]:
    return {
        "coordinate_frame": "local_odometry_m",
        "localization_required": True,
        "schema_note": "Map coordinates are local meters and need startup localization before patrol.",
    }
