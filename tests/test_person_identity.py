"""Tests for face-to-body identity fusion and persistence."""

from __future__ import annotations

import unittest

from go2_local_brain.autonomy.perception import Detection
from go2_local_brain.autonomy.person_identity import PersonIdentityTracker


def _person(x: float, *, confidence: float = 0.9) -> Detection:
    return Detection("person", confidence, x=x, y=240, width=160, height=400)


def _face(label: str, left: float, score: float = 0.9) -> dict[str, object]:
    return {"label": label, "score": score, "x": left, "y": 0.14, "w": 0.08, "h": 0.12}


class PersonIdentityTrackerTests(unittest.TestCase):
    def test_maps_two_faces_to_two_bodies(self) -> None:
        tracker = PersonIdentityTracker()
        tracker.update_bodies([_person(160), _person(480)], 640, 480, now=0.0)
        tracker.observe_faces([_face("Cooper", 0.20), _face("Alex", 0.70)], now=0.1)
        labels = {track.label for track in tracker.tracks(now=0.1)}
        self.assertEqual(labels, {"Cooper", "Alex"})

    def test_known_identity_survives_unknown_face_and_head_turn(self) -> None:
        tracker = PersonIdentityTracker(identity_ttl_s=75.0)
        tracker.update_bodies([_person(320)], 640, 480, now=0.0)
        tracker.observe_faces([_face("Cooper", 0.46)], now=0.1)
        tracker.update_bodies([_person(325)], 640, 480, now=60.0)
        tracker.observe_faces([_face("unknown", 0.47)], now=60.0)
        self.assertEqual(tracker.tracks(now=60.0)[0].label, "Cooper")

    def test_unknown_face_keeps_body_unknown(self) -> None:
        tracker = PersonIdentityTracker()
        tracker.update_bodies([_person(320)], 640, 480, now=0.0)
        tracker.observe_faces([_face("unknown", 0.46)], now=0.1)
        self.assertEqual(tracker.tracks(now=0.1)[0].label, "unknown")

    def test_identity_expires_after_lease(self) -> None:
        tracker = PersonIdentityTracker(identity_ttl_s=10.0)
        tracker.update_bodies([_person(320)], 640, 480, now=0.0)
        tracker.observe_faces([_face("Cooper", 0.46)], now=0.1)
        tracker.update_bodies([_person(320)], 640, 480, now=11.0)
        self.assertEqual(tracker.tracks(now=11.0)[0].label, "unknown")

    def test_find_returns_named_body_for_specific_follow(self) -> None:
        tracker = PersonIdentityTracker()
        tracker.update_bodies([_person(320)], 640, 480, now=0.0)
        tracker.observe_faces([_face("Cooper", 0.46)], now=0.1)
        self.assertIsNotNone(tracker.find("cooper", now=0.1))
        self.assertIsNone(tracker.find("Alex", now=0.1))


if __name__ == "__main__":
    unittest.main()
