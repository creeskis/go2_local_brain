"""Tests for the optional browser viewer helpers."""

from __future__ import annotations

import unittest
import struct

from go2_local_brain.viewer import (
    _coerce_position_values,
    _decimate,
    _float32_triplets_from_byte_values,
    _lidar_payload_from_message,
    _points_from_positions,
    _xyz_triplets,
)


class LidarPayloadTests(unittest.TestCase):
    def test_xyz_triplets_ignores_incomplete_tail(self) -> None:
        self.assertEqual(_xyz_triplets([1, 2, 3, 4]), [[1.0, 2.0, 3.0]])

    def test_lidar_payload_uses_decoded_positions_shape(self) -> None:
        message = {"data": {"stamp": 123, "data": {"positions": [1, 0, 0, 0, 2, 0]}}}
        payload = _lidar_payload_from_message(message, max_points=10)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["points"], [[-0.5, 0.0, -1.0], [0.5, 0.0, 1.0]])
        self.assertEqual(payload["point_count"], 2)
        self.assertEqual(payload["source_point_count"], 2)

    def test_lidar_payload_accepts_direct_positions_shape(self) -> None:
        message = {"data": {"stamp": 123, "positions": [1, 2, 3]}}
        payload = _lidar_payload_from_message(message, max_points=10)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["points"], [[0.0, 0.0, 0.0]])

    def test_lidar_payload_accepts_numpy_like_float32_bytes(self) -> None:
        class FakeArray:
            def tolist(self) -> list[int]:
                return list(struct.pack("<ffffff", 1.0, 2.0, 3.0, 4.0, 5.0, 6.0))

        message = {"data": {"stamp": 123, "data": {"positions": FakeArray()}}}
        payload = _lidar_payload_from_message(message, max_points=10)
        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["points"], [[1.5, 0.0, -1.5], [-1.5, 3.0, 1.5]])

    def test_coerce_positions_accepts_bytearray(self) -> None:
        self.assertEqual(_coerce_position_values(bytearray([1, 2, 3])), [1, 2, 3])

    def test_float32_triplets_from_byte_values(self) -> None:
        values = list(struct.pack("<ffffff", 1.0, 2.0, 3.0, 4.0, 5.0, 6.0))
        self.assertEqual(_float32_triplets_from_byte_values(values), [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])

    def test_points_from_positions_keeps_short_numeric_lists_as_xyz(self) -> None:
        points, source_count = _points_from_positions([1, 2, 3, 4, 5, 6])
        self.assertEqual(points, [[1.0, 2.0, 3.0], [4.0, 5.0, 6.0]])
        self.assertEqual(source_count, 2)

    def test_decimate_caps_point_count(self) -> None:
        points = [[float(i), 0.0, 0.0] for i in range(10)]
        self.assertEqual(len(_decimate(points, 4)), 4)


if __name__ == "__main__":
    unittest.main()
