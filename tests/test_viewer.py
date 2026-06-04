"""Tests for the optional browser viewer helpers."""

from __future__ import annotations

import unittest

from go2_local_brain.viewer import _decimate, _lidar_payload_from_message, _xyz_triplets


class LidarPayloadTests(unittest.TestCase):
    def test_xyz_triplets_ignores_incomplete_tail(self) -> None:
        self.assertEqual(_xyz_triplets([1, 2, 3, 4]), [[1.0, 2.0, 3.0]])

    def test_lidar_payload_uses_decoded_positions_shape(self) -> None:
        message = {"data": {"stamp": 123, "data": {"positions": [1, 0, 0, 0, 2, 0]}}}
        payload = _lidar_payload_from_message(message, max_points=10)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["points"], [[1.0, 0.0, 0.0], [0.0, 2.0, 0.0]])
        self.assertEqual(payload["point_count"], 2)
        self.assertEqual(payload["source_point_count"], 2)

    def test_decimate_caps_point_count(self) -> None:
        points = [[float(i), 0.0, 0.0] for i in range(10)]
        self.assertEqual(len(_decimate(points, 4)), 4)


if __name__ == "__main__":
    unittest.main()
