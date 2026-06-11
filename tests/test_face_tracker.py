"""Tests for FaceTracker: stable ids, association, label smoothing."""

from __future__ import annotations

import unittest

from go2_local_brain.autonomy.face_id import UNKNOWN_LABEL, IdentifiedFace
from go2_local_brain.autonomy.face_tracker import FaceTracker


def _face(label: str, x: float, y: float, score: float = 0.9) -> IdentifiedFace:
    return IdentifiedFace(label=label, score=score, x=x, y=y, width=40, height=40)


class FaceTrackerTests(unittest.TestCase):
    def test_same_face_keeps_track_id(self) -> None:
        tr = FaceTracker(match_distance_px=80)
        t0 = tr.update([_face("cooper", 100, 100)], now=0.0)
        first_id = t0[0].track_id
        # Small movement -> same track.
        t1 = tr.update([_face("cooper", 110, 105)], now=0.1)
        self.assertEqual(t1[0].track_id, first_id)
        self.assertEqual(t1[0].hits, 2)

    def test_distant_face_gets_new_id(self) -> None:
        tr = FaceTracker(match_distance_px=50)
        a = tr.update([_face("cooper", 100, 100)], now=0.0)
        b = tr.update([_face("cooper", 400, 400)], now=0.1)
        self.assertNotEqual(a[0].track_id, b[0].track_id)

    def test_label_majority_vote(self) -> None:
        # Noisy labels on the same track -> smoothed to the majority.
        tr = FaceTracker(match_distance_px=80)
        tr.update([_face("cooper", 100, 100)], now=0.0)
        tr.update([_face(UNKNOWN_LABEL, 102, 100)], now=0.1)
        tr.update([_face("cooper", 104, 100)], now=0.2)
        tracks = tr.update([_face("cooper", 106, 100)], now=0.3)
        self.assertEqual(tracks[0].label, "cooper")
        self.assertTrue(tracks[0].is_known)

    def test_known_label_wins_tie_against_unknown(self) -> None:
        tr = FaceTracker(match_distance_px=80)
        tr.update([_face("cooper", 100, 100)], now=0.0)
        tracks = tr.update([_face(UNKNOWN_LABEL, 102, 100)], now=0.1)
        # 1 vote each; known should win the tie.
        self.assertEqual(tracks[0].label, "cooper")

    def test_stale_track_expires(self) -> None:
        tr = FaceTracker(max_age_s=0.5, match_distance_px=80)
        tr.update([_face("cooper", 100, 100)], now=0.0)
        # No update for >0.5s -> expired on next update.
        live = tr.update([], now=1.0)
        self.assertEqual(live, [])

    def test_confident_tracks_need_min_hits(self) -> None:
        tr = FaceTracker(match_distance_px=80, min_hits_for_confident=3)
        tr.update([_face("cooper", 100, 100)], now=0.0)
        tr.update([_face("cooper", 101, 100)], now=0.1)
        self.assertEqual(tr.confident_tracks(), [])  # only 2 hits
        tr.update([_face("cooper", 102, 100)], now=0.2)
        self.assertEqual(len(tr.confident_tracks()), 1)

    def test_two_simultaneous_faces_get_distinct_ids(self) -> None:
        tr = FaceTracker(match_distance_px=80)
        tracks = tr.update([_face("a", 100, 100), _face("b", 500, 100)], now=0.0)
        ids = {t.track_id for t in tracks}
        self.assertEqual(len(ids), 2)


if __name__ == "__main__":
    unittest.main()
