"""Fuse person boxes with FaceID and retain identity across face loss."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Sequence

from .face_id import UNKNOWN_LABEL
from .perception import Detection


@dataclass
class PersonTrack:
    track_id: int
    left: float
    top: float
    width: float
    height: float
    confidence: float
    last_seen_ts: float
    detection: Detection
    label: str = UNKNOWN_LABEL
    identity_score: float = 0.0
    identity_seen_ts: float = 0.0

    @property
    def is_known(self) -> bool:
        return self.label != UNKNOWN_LABEL

    def to_dict(self) -> dict[str, Any]:
        return {
            "track_id": self.track_id,
            "x": self.left,
            "y": self.top,
            "w": self.width,
            "h": self.height,
            "label": self.label,
            "known": self.is_known,
            "score": self.identity_score if self.is_known else self.confidence,
            "person_confidence": self.confidence,
            "identity_age_s": None if not self.is_known else max(0.0, time.time() - self.identity_seen_ts),
        }


class PersonIdentityTracker:
    """Track bodies, attach faces to them, and keep names through head turns."""

    def __init__(self, *, body_ttl_s: float = 2.0, identity_ttl_s: float = 75.0) -> None:
        self._body_ttl_s = max(0.2, body_ttl_s)
        self._identity_ttl_s = max(1.0, identity_ttl_s)
        self._tracks: dict[int, PersonTrack] = {}
        self._next_id = 1

    def update_bodies(
        self,
        detections: Sequence[Detection],
        frame_width: int | None,
        frame_height: int | None,
        *,
        now: float | None = None,
    ) -> list[PersonTrack]:
        now = time.time() if now is None else now
        bodies = [
            (detection, box)
            for detection in detections
            if detection.is_human()
            for box in [_normalized_box(detection, frame_width, frame_height)]
            if box is not None
        ]
        unmatched_tracks = set(self._tracks)
        unmatched_bodies = set(range(len(bodies)))
        candidates: list[tuple[float, int, int]] = []
        for track_id, track in self._tracks.items():
            for body_index, (_, box) in enumerate(bodies):
                overlap = _iou(_track_box(track), box)
                distance = _center_distance(_track_box(track), box)
                if overlap >= 0.03 or distance <= 0.28:
                    candidates.append(((1.0 - overlap) + distance, track_id, body_index))
        for _, track_id, body_index in sorted(candidates):
            if track_id not in unmatched_tracks or body_index not in unmatched_bodies:
                continue
            detection, box = bodies[body_index]
            self._update_track(self._tracks[track_id], detection, box, now)
            unmatched_tracks.remove(track_id)
            unmatched_bodies.remove(body_index)
        for body_index in sorted(unmatched_bodies):
            detection, box = bodies[body_index]
            self._spawn(detection, box, now)
        self._expire(now)
        return self.tracks(now=now)

    def observe_faces(self, faces: Sequence[dict[str, Any]], *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        self._expire(now)
        for face in faces:
            label = str(face.get("label") or UNKNOWN_LABEL).strip() or UNKNOWN_LABEL
            track = self._body_for_face(face)
            if track is None or label == UNKNOWN_LABEL:
                continue
            # One live body owns a known identity. This prevents a stale track
            # retaining the same name after that person is reacquired nearby.
            for other in self._tracks.values():
                if other.track_id != track.track_id and other.label.casefold() == label.casefold():
                    other.label = UNKNOWN_LABEL
                    other.identity_score = 0.0
                    other.identity_seen_ts = 0.0
            track.label = label
            track.identity_score = float(face.get("score") or 0.0)
            track.identity_seen_ts = now

    def tracks(self, *, now: float | None = None) -> list[PersonTrack]:
        now = time.time() if now is None else now
        self._expire(now)
        return sorted(self._tracks.values(), key=lambda track: track.last_seen_ts, reverse=True)

    def find(self, label: str, *, now: float | None = None) -> PersonTrack | None:
        key = label.strip().casefold()
        if not key:
            return None
        for track in self.tracks(now=now):
            if track.label.casefold() == key:
                return track
        return None

    def _body_for_face(self, face: dict[str, Any]) -> PersonTrack | None:
        left = float(face.get("x") or 0.0)
        top = float(face.get("y") or 0.0)
        width = float(face.get("w") or 0.0)
        height = float(face.get("h") or 0.0)
        cx, cy = left + width / 2.0, top + height / 2.0
        candidates: list[tuple[float, PersonTrack]] = []
        for track in self._tracks.values():
            margin_x = track.width * 0.12
            within_x = track.left - margin_x <= cx <= track.left + track.width + margin_x
            within_y = track.top - track.height * 0.12 <= cy <= track.top + track.height * 0.68
            if not (within_x and within_y):
                continue
            expected_x = track.left + track.width / 2.0
            expected_y = track.top + track.height * 0.18
            distance = math.hypot(cx - expected_x, cy - expected_y)
            candidates.append((distance, track))
        return min(candidates, key=lambda item: item[0])[1] if candidates else None

    def _update_track(
        self,
        track: PersonTrack,
        detection: Detection,
        box: tuple[float, float, float, float],
        now: float,
    ) -> None:
        track.left, track.top, track.width, track.height = box
        track.confidence = detection.confidence
        track.last_seen_ts = now
        track.detection = detection

    def _spawn(
        self,
        detection: Detection,
        box: tuple[float, float, float, float],
        now: float,
    ) -> None:
        self._tracks[self._next_id] = PersonTrack(
            track_id=self._next_id,
            left=box[0],
            top=box[1],
            width=box[2],
            height=box[3],
            confidence=detection.confidence,
            last_seen_ts=now,
            detection=detection,
        )
        self._next_id += 1

    def _expire(self, now: float) -> None:
        for track_id in [
            track_id
            for track_id, track in self._tracks.items()
            if now - track.last_seen_ts > self._body_ttl_s
        ]:
            del self._tracks[track_id]
        for track in self._tracks.values():
            if track.is_known and now - track.identity_seen_ts > self._identity_ttl_s:
                track.label = UNKNOWN_LABEL
                track.identity_score = 0.0
                track.identity_seen_ts = 0.0


def _normalized_box(
    detection: Detection,
    frame_width: int | None,
    frame_height: int | None,
) -> tuple[float, float, float, float] | None:
    if None in {detection.x, detection.y, detection.width, detection.height}:
        return None
    x, y = float(detection.x), float(detection.y)
    width, height = float(detection.width), float(detection.height)
    if x > 1.0 or y > 1.0 or width > 1.0 or height > 1.0:
        if not frame_width or not frame_height:
            return None
        x, width = x / frame_width, width / frame_width
        y, height = y / frame_height, height / frame_height
    left = max(0.0, min(1.0, x - width / 2.0))
    top = max(0.0, min(1.0, y - height / 2.0))
    return left, top, min(width, 1.0 - left), min(height, 1.0 - top)


def _track_box(track: PersonTrack) -> tuple[float, float, float, float]:
    return track.left, track.top, track.width, track.height


def _center_distance(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    return math.hypot((a[0] + a[2] / 2.0) - (b[0] + b[2] / 2.0), (a[1] + a[3] / 2.0) - (b[1] + b[3] / 2.0))


def _iou(
    a: tuple[float, float, float, float],
    b: tuple[float, float, float, float],
) -> float:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    union = a[2] * a[3] + b[2] * b[3] - intersection
    return intersection / union if union > 0.0 else 0.0
