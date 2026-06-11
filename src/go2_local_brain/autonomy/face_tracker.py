"""Track identified faces across frames with stable IDs + label smoothing.

A single-frame face identification is noisy: the embedding can flip between
"cooper" and "unknown" frame to frame, and YOLO/Haar boxes jitter. This
module assigns each face a stable ``track_id`` by associating detections to
existing tracks via centroid distance, and smooths the label with a
majority vote over the track's recent history.

Pure geometry + counting — no ML, no hardware — so it's fully unit-tested.
Feed it ``IdentifiedFace`` objects from ``face_id.FaceIdentifier`` each frame.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional, Sequence

from .face_id import UNKNOWN_LABEL, IdentifiedFace


@dataclass
class FaceTrack:
    """One face followed across frames."""

    track_id: int
    x: float
    y: float
    width: float
    height: float
    last_seen_ts: float
    first_seen_ts: float
    hits: int = 1
    # Vote history of per-frame labels; the smoothed label is the mode.
    _label_votes: Counter = field(default_factory=Counter)
    last_score: float = 0.0

    @property
    def label(self) -> str:
        """Smoothed identity: the most-voted label across this track's life.

        Ties and empty history fall back to 'unknown'. Known labels beat
        'unknown' on a tie so a couple of confident hits win over noise.
        """
        if not self._label_votes:
            return UNKNOWN_LABEL
        # Sort by (count, is_known) so a known label wins ties vs unknown.
        best = max(
            self._label_votes.items(),
            key=lambda kv: (kv[1], kv[0] != UNKNOWN_LABEL),
        )
        return best[0]

    @property
    def is_known(self) -> bool:
        return self.label != UNKNOWN_LABEL

    def centroid(self) -> tuple[float, float]:
        return (self.x, self.y)

    def to_dict(self) -> dict[str, object]:
        return {
            "track_id": self.track_id,
            "label": self.label,
            "is_known": self.is_known,
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "width": round(self.width, 1),
            "height": round(self.height, 1),
            "hits": self.hits,
            "score": round(self.last_score, 3),
        }


class FaceTracker:
    """Associates per-frame identified faces into persistent tracks."""

    def __init__(
        self,
        *,
        max_age_s: float = 1.5,
        match_distance_px: float = 120.0,
        min_hits_for_confident: int = 3,
    ) -> None:
        self._tracks: dict[int, FaceTrack] = {}
        self._next_id = 1
        self._max_age_s = max_age_s
        self._match_distance = match_distance_px
        self._min_hits = min_hits_for_confident

    def update(self, faces: Sequence[IdentifiedFace], *, now: Optional[float] = None) -> list[FaceTrack]:
        """Ingest this frame's faces; return the live (non-expired) tracks."""
        now = time.monotonic() if now is None else now

        # Greedy nearest-centroid association. For the handful of faces a
        # robot sees at once, greedy is plenty; no Hungarian needed.
        unmatched = list(faces)
        used_tracks: set[int] = set()

        for face in list(unmatched):
            track = self._closest_track(face, used_tracks)
            if track is None:
                continue
            used_tracks.add(track.track_id)
            self._absorb(track, face, now)
            unmatched.remove(face)

        # Remaining faces start new tracks.
        for face in unmatched:
            self._spawn(face, now)

        # Expire stale tracks.
        for track_id in [tid for tid, t in self._tracks.items() if now - t.last_seen_ts > self._max_age_s]:
            del self._tracks[track_id]

        return self.tracks()

    def tracks(self) -> list[FaceTrack]:
        """Live tracks, most-recently-seen first."""
        return sorted(self._tracks.values(), key=lambda t: t.last_seen_ts, reverse=True)

    def confident_tracks(self) -> list[FaceTrack]:
        """Tracks with enough hits to trust the smoothed label."""
        return [t for t in self.tracks() if t.hits >= self._min_hits]

    def reset(self) -> None:
        self._tracks.clear()
        self._next_id = 1

    # ------------------------------------------------------------- internal

    def _closest_track(self, face: IdentifiedFace, used: set[int]) -> Optional[FaceTrack]:
        best: Optional[FaceTrack] = None
        best_dist = self._match_distance
        for track in self._tracks.values():
            if track.track_id in used:
                continue
            d = math.hypot(face.x - track.x, face.y - track.y)
            if d < best_dist:
                best_dist = d
                best = track
        return best

    def _absorb(self, track: FaceTrack, face: IdentifiedFace, now: float) -> None:
        track.x = face.x
        track.y = face.y
        track.width = face.width
        track.height = face.height
        track.last_seen_ts = now
        track.hits += 1
        track.last_score = face.score
        track._label_votes[face.label] += 1

    def _spawn(self, face: IdentifiedFace, now: float) -> None:
        track = FaceTrack(
            track_id=self._next_id,
            x=face.x,
            y=face.y,
            width=face.width,
            height=face.height,
            last_seen_ts=now,
            first_seen_ts=now,
            last_score=face.score,
        )
        track._label_votes[face.label] += 1
        self._tracks[self._next_id] = track
        self._next_id += 1
