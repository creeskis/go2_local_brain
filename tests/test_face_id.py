"""Tests for face_id: cosine match, database, identifier (no ML backend)."""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from go2_local_brain.autonomy.face_id import (
    UNKNOWN_LABEL,
    FaceDatabase,
    FaceIdentifier,
    InsightFaceEmbedder,
    NullFaceEmbedder,
    build_face_embedder,
    cosine_similarity,
)


class CosineTests(unittest.TestCase):
    def test_identical_vectors(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1, 0, 0], [1, 0, 0]), 1.0)

    def test_orthogonal_vectors(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1, 0], [0, 1]), 0.0)

    def test_opposite_vectors(self) -> None:
        self.assertAlmostEqual(cosine_similarity([1, 0], [-1, 0]), -1.0)

    def test_mismatched_length_is_zero(self) -> None:
        self.assertEqual(cosine_similarity([1, 0], [1, 0, 0]), 0.0)

    def test_zero_vector_is_zero(self) -> None:
        self.assertEqual(cosine_similarity([0, 0], [1, 1]), 0.0)


class FaceDatabaseTests(unittest.TestCase):
    def test_enroll_and_identify_known(self) -> None:
        db = FaceDatabase(match_threshold=0.9)
        db.enroll("cooper", [1.0, 0.0, 0.0])
        db.enroll("alex", [0.0, 1.0, 0.0])
        match = db.identify([0.95, 0.05, 0.0])
        self.assertEqual(match.label, "cooper")
        self.assertTrue(match.is_known)

    def test_identify_below_threshold_is_unknown(self) -> None:
        db = FaceDatabase(match_threshold=0.99)
        db.enroll("cooper", [1.0, 0.0, 0.0])
        match = db.identify([0.0, 1.0, 0.0])  # orthogonal -> score 0
        self.assertEqual(match.label, UNKNOWN_LABEL)
        self.assertFalse(match.is_known)

    def test_enroll_rejects_dim_mismatch(self) -> None:
        db = FaceDatabase()
        db.enroll("cooper", [1.0, 0.0])
        with self.assertRaises(ValueError):
            db.enroll("cooper", [1.0, 0.0, 0.0])

    def test_enroll_rejects_empty(self) -> None:
        db = FaceDatabase()
        with self.assertRaises(ValueError):
            db.enroll("", [1.0])
        with self.assertRaises(ValueError):
            db.enroll("x", [])

    def test_remove_and_count(self) -> None:
        db = FaceDatabase()
        db.enroll("cooper", [1.0, 0.0])
        db.enroll("cooper", [0.9, 0.1])
        self.assertEqual(db.count("cooper"), 2)
        self.assertTrue(db.remove("cooper"))
        self.assertEqual(db.count("cooper"), 0)
        self.assertFalse(db.remove("nobody"))

    def test_multiple_embeddings_use_best_match(self) -> None:
        # Two enrollment shots; identify should match the closer one.
        db = FaceDatabase(match_threshold=0.8)
        db.enroll("cooper", [1.0, 0.0, 0.0])
        db.enroll("cooper", [0.0, 0.0, 1.0])
        match = db.identify([0.0, 0.05, 0.99])
        self.assertEqual(match.label, "cooper")

    def test_save_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "faces.json"
            db = FaceDatabase(match_threshold=0.9)
            db.enroll("cooper", [1.0, 0.0, 0.0])
            db.enroll("alex", [0.0, 1.0, 0.0])
            db.save(path)

            restored = FaceDatabase.load(path, match_threshold=0.9)
            self.assertEqual(restored.labels(), ["alex", "cooper"])
            self.assertEqual(restored.dim, 3)
            self.assertEqual(restored.identify([0.99, 0.0, 0.0]).label, "cooper")

    def test_load_or_empty_on_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db = FaceDatabase.load_or_empty(Path(tmp) / "nope.json")
            self.assertEqual(db.labels(), [])


class FaceIdentifierTests(unittest.TestCase):
    def test_null_embedder_yields_unknown(self) -> None:
        ident = FaceIdentifier(NullFaceEmbedder(), FaceDatabase())
        faces = ident.identify_faces(object(), [(0, 0, 10, 10)])
        self.assertEqual(len(faces), 1)
        self.assertEqual(faces[0].label, UNKNOWN_LABEL)
        # Box center + size computed from corner box.
        self.assertEqual(faces[0].x, 5.0)
        self.assertEqual(faces[0].width, 10.0)

    def test_enroll_from_image_false_for_null_embedder(self) -> None:
        ident = FaceIdentifier(NullFaceEmbedder(), FaceDatabase())
        ok = ident.enroll_from_image("cooper", object(), (0, 0, 10, 10))
        self.assertFalse(ok)

    def test_identifier_with_fake_embedder(self) -> None:
        # A fake embedder returns a fixed vector so we exercise the matching
        # path end-to-end without dlib/onnx.
        class FakeEmbedder(NullFaceEmbedder):
            def embed(self, image_rgb, box):
                return [1.0, 0.0, 0.0]

        db = FaceDatabase(match_threshold=0.9)
        db.enroll("cooper", [1.0, 0.0, 0.0])
        ident = FaceIdentifier(FakeEmbedder(), db)
        faces = ident.identify_faces(object(), [(0, 0, 20, 20)])
        self.assertEqual(faces[0].label, "cooper")
        self.assertTrue(faces[0].is_known)

    def test_identifier_batches_multiple_faces(self) -> None:
        class BatchEmbedder(NullFaceEmbedder):
            def __init__(self) -> None:
                self.calls = 0

            def embed_many(self, image_rgb, boxes):
                self.calls += 1
                return [[1.0, 0.0], [0.0, 1.0]]

        embedder = BatchEmbedder()
        db = FaceDatabase(match_threshold=0.9)
        db.enroll("cooper", [1.0, 0.0])
        db.enroll("alex", [0.0, 1.0])
        ident = FaceIdentifier(embedder, db)
        faces = ident.identify_faces(object(), [(0, 0, 20, 20), (30, 0, 50, 20)])
        self.assertEqual(embedder.calls, 1)
        self.assertEqual([face.label for face in faces], ["cooper", "alex"])


class EmbedderFactoryTests(unittest.TestCase):
    def test_null_backend(self) -> None:
        self.assertIsInstance(build_face_embedder("null"), NullFaceEmbedder)

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            build_face_embedder("not-a-backend")


class InsightFaceBatchTests(unittest.TestCase):
    def test_two_faces_are_mapped_in_one_model_pass(self) -> None:
        import numpy as np
        from PIL import Image

        class FakeFace:
            def __init__(self, bbox, embedding):
                self.bbox = bbox
                self.normed_embedding = np.asarray(embedding)

        class FakeApp:
            def __init__(self) -> None:
                self.calls = 0

            def get(self, _image):
                self.calls += 1
                return [
                    FakeFace([10, 10, 40, 40], [1.0, 0.0]),
                    FakeFace([110, 10, 140, 40], [0.0, 1.0]),
                ]

        embedder = InsightFaceEmbedder()
        embedder._app = FakeApp()
        embedder._np = np
        embeddings = embedder.embed_many(
            Image.new("RGB", (160, 80)),
            [(5, 5, 45, 45), (105, 5, 145, 45)],
        )
        self.assertEqual(embedder._app.calls, 1)
        self.assertEqual(embeddings, [[1.0, 0.0], [0.0, 1.0]])


if __name__ == "__main__":
    unittest.main()
