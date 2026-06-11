"""Face identification: embed faces, store labeled embeddings, match by similarity.

Design
------
Three layers, each independently testable:

1. ``FaceEmbedder`` — turns a face crop into a fixed-length vector. The ML
   backends (InsightFace / face_recognition) are *lazy-imported* so the rest
   of the app — and the test suite — runs without dlib, onnxruntime, or any
   model file present. ``NullFaceEmbedder`` is the default and returns None.

2. ``FaceDatabase`` — pure-Python store of ``{label: [embedding, ...]}``,
   persisted as JSON. Matching is cosine similarity, implemented here in
   plain Python so enrollment / identification logic is fully testable with
   hand-written vectors and no ML backend at all.

3. ``FaceIdentifier`` — glues an embedder to a database: given an image and
   the face boxes (from YOLO/Haar detection upstream), returns labeled
   ``IdentifiedFace`` results. Faces below the match threshold are labeled
   ``"unknown"``.

The upstream perception layer already produces face *boxes* (OpenCV Haar in
``perception._face_detections_from_image``). This module adds *identity* on
top of those boxes.
"""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

log = logging.getLogger(__name__)

# A face embedding is just a list of floats (128-dim for dlib, 512 for
# InsightFace). We keep it as a plain list so it serializes to JSON and
# stays backend-agnostic.
Embedding = list[float]

# Default: cosine similarity above this counts as a match. Tunable per
# backend; dlib distances and InsightFace cosines have different scales,
# so callers should override based on their embedder.
DEFAULT_MATCH_THRESHOLD = 0.45

UNKNOWN_LABEL = "unknown"


# --------------------------------------------------------------------- math


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity of two equal-length vectors. Range [-1, 1].

    Returns 0.0 for mismatched lengths or zero-norm vectors rather than
    raising — callers treat 0.0 as "no match".
    """
    if len(a) != len(b) or not a:
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# ----------------------------------------------------------------- results


@dataclass(frozen=True)
class FaceMatch:
    """Result of matching one embedding against the database."""

    label: str
    score: float
    is_known: bool


@dataclass(frozen=True)
class IdentifiedFace:
    """A face box plus the identity assigned to it.

    Box fields mirror ``perception.Detection`` (center x/y + width/height in
    pixel coords) so this drops into the same overlay/normalization paths.
    """

    label: str
    score: float
    x: float
    y: float
    width: float
    height: float

    @property
    def is_known(self) -> bool:
        return self.label != UNKNOWN_LABEL


# ---------------------------------------------------------------- database


@dataclass
class FaceDatabase:
    """Labeled embeddings, persisted as JSON.

    File format::

        {
          "dim": 128,
          "people": {
            "cooper": [[...128 floats...], [...]],
            "alex":   [[...], [...]]
          }
        }
    """

    people: dict[str, list[Embedding]] = field(default_factory=dict)
    dim: Optional[int] = None
    match_threshold: float = DEFAULT_MATCH_THRESHOLD

    # ------------------------------------------------------------- enroll

    def enroll(self, label: str, embedding: Embedding) -> None:
        """Add one embedding for ``label``. Validates dimensionality."""
        label = label.strip()
        if not label:
            raise ValueError("label must be non-empty")
        if not embedding:
            raise ValueError("embedding must be non-empty")
        if self.dim is None:
            self.dim = len(embedding)
        elif len(embedding) != self.dim:
            raise ValueError(
                f"embedding dim {len(embedding)} != database dim {self.dim}"
            )
        self.people.setdefault(label, []).append(list(embedding))

    def remove(self, label: str) -> bool:
        """Drop all embeddings for ``label``. Returns True if it existed."""
        return self.people.pop(label.strip(), None) is not None

    def labels(self) -> list[str]:
        return sorted(self.people.keys())

    def count(self, label: str) -> int:
        return len(self.people.get(label.strip(), []))

    # ----------------------------------------------------------- identify

    def identify(self, embedding: Embedding, *, threshold: Optional[float] = None) -> FaceMatch:
        """Best match for ``embedding``. Below threshold -> unknown.

        Compares against every stored embedding (max similarity per label
        wins — robust to a few bad enrollment shots).
        """
        thr = self.match_threshold if threshold is None else threshold
        best_label = UNKNOWN_LABEL
        best_score = 0.0
        for label, embeddings in self.people.items():
            for stored in embeddings:
                score = cosine_similarity(embedding, stored)
                if score > best_score:
                    best_score = score
                    best_label = label
        if best_score < thr:
            return FaceMatch(label=UNKNOWN_LABEL, score=best_score, is_known=False)
        return FaceMatch(label=best_label, score=best_score, is_known=True)

    # --------------------------------------------------------------- disk

    @classmethod
    def default_path(cls) -> Path:
        import os

        override = os.getenv("GO2_FACE_DB")
        if override:
            return Path(override).expanduser()
        return Path.home() / ".config" / "go2_local_brain" / "faces.json"

    def save(self, path: Optional[Path] = None) -> Path:
        path = path or self.default_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"dim": self.dim, "people": self.people}
        with path.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return path

    @classmethod
    def load(cls, path: Optional[Path] = None, *, match_threshold: float = DEFAULT_MATCH_THRESHOLD) -> "FaceDatabase":
        path = path or cls.default_path()
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        people = {
            str(label): [[float(v) for v in emb] for emb in embs]
            for label, embs in (raw.get("people") or {}).items()
        }
        return cls(people=people, dim=raw.get("dim"), match_threshold=match_threshold)

    @classmethod
    def load_or_empty(cls, path: Optional[Path] = None, *, match_threshold: float = DEFAULT_MATCH_THRESHOLD) -> "FaceDatabase":
        path = path or cls.default_path()
        try:
            return cls.load(path, match_threshold=match_threshold)
        except (FileNotFoundError, json.JSONDecodeError):
            return cls(match_threshold=match_threshold)


# ----------------------------------------------------------------- embedders


class FaceEmbedder:
    """Abstract: turn a face crop into an embedding vector."""

    @property
    def dim(self) -> Optional[int]:  # pragma: no cover - trivial
        return None

    def embed(self, image_rgb: Any, box: tuple[int, int, int, int]) -> Optional[Embedding]:
        """Return an embedding for the face at ``box`` (x1,y1,x2,y2) in image.

        Returns None if no face could be embedded.
        """
        raise NotImplementedError


class NullFaceEmbedder(FaceEmbedder):
    """Default: embeds nothing. Used when no face backend is configured."""

    def embed(self, image_rgb: Any, box: tuple[int, int, int, int]) -> Optional[Embedding]:
        return None


class FaceRecognitionEmbedder(FaceEmbedder):
    """dlib-based ``face_recognition`` backend. 128-dim. Lazy-imported.

    Good CPU path; recommended threshold ~0.92 cosine (dlib embeddings are
    L2-normalized-ish but tuned for distance — adjust on your data).
    """

    def __init__(self) -> None:
        self._fr: Any = None

    def _ensure(self) -> None:
        if self._fr is not None:
            return
        try:
            import face_recognition  # type: ignore
            import numpy as np  # type: ignore
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise RuntimeError(
                "face_recognition + numpy required for FaceRecognitionEmbedder; "
                "pip install face_recognition (needs cmake + dlib)"
            ) from exc
        self._fr = face_recognition
        self._np = np

    @property
    def dim(self) -> int:
        return 128

    def embed(self, image_rgb: Any, box: tuple[int, int, int, int]) -> Optional[Embedding]:
        self._ensure()
        x1, y1, x2, y2 = box
        # face_recognition expects (top, right, bottom, left).
        locations = [(y1, x2, y2, x1)]
        arr = self._np.asarray(image_rgb)
        encodings = self._fr.face_encodings(arr, known_face_locations=locations)
        if not encodings:
            return None
        return [float(v) for v in encodings[0]]


class InsightFaceEmbedder(FaceEmbedder):
    """InsightFace ONNX backend. 512-dim. Lazy-imported. Best on Jetson GPU.

    Uses the bundled detector for alignment, so we pass the full image and
    let it find the largest face near ``box``.
    """

    def __init__(self, model_name: str = "buffalo_l", providers: Optional[Sequence[str]] = None) -> None:
        self._app: Any = None
        self._model_name = model_name
        self._providers = list(providers) if providers else None

    def _ensure(self) -> None:
        if self._app is not None:
            return
        try:
            from insightface.app import FaceAnalysis  # type: ignore
            import numpy as np  # type: ignore
        except ImportError as exc:  # pragma: no cover - env-dependent
            raise RuntimeError(
                "insightface + numpy required for InsightFaceEmbedder; "
                "pip install insightface onnxruntime (or onnxruntime-gpu)"
            ) from exc
        providers = self._providers or ["CUDAExecutionProvider", "CPUExecutionProvider"]
        app = FaceAnalysis(name=self._model_name, providers=providers)
        app.prepare(ctx_id=0, det_size=(320, 320))
        self._app = app
        self._np = np

    @property
    def dim(self) -> int:
        return 512

    def embed(self, image_rgb: Any, box: tuple[int, int, int, int]) -> Optional[Embedding]:
        self._ensure()
        arr = self._np.asarray(image_rgb)
        faces = self._app.get(arr)
        if not faces:
            return None
        # Pick the detected face whose center is closest to the requested box.
        cx = (box[0] + box[2]) / 2
        cy = (box[1] + box[3]) / 2

        def dist(f: Any) -> float:
            fx1, fy1, fx2, fy2 = f.bbox
            return (cx - (fx1 + fx2) / 2) ** 2 + (cy - (fy1 + fy2) / 2) ** 2

        best = min(faces, key=dist)
        emb = getattr(best, "normed_embedding", None)
        if emb is None:
            emb = getattr(best, "embedding", None)
        if emb is None:
            return None
        return [float(v) for v in emb]


def build_face_embedder(backend: str = "null", **kwargs: Any) -> FaceEmbedder:
    """Construct the embedder named by the operator.

    Backends: ``null`` (default), ``face_recognition``, ``insightface``.
    """
    key = backend.strip().lower()
    if key in {"null", "none", ""}:
        return NullFaceEmbedder()
    if key in {"face_recognition", "dlib", "fr"}:
        return FaceRecognitionEmbedder()
    if key in {"insightface", "insight", "arcface"}:
        return InsightFaceEmbedder(**kwargs)
    raise ValueError(f"unknown face embedder backend: {backend!r}")


# ----------------------------------------------------------------- identifier


class FaceIdentifier:
    """Glue: embed face boxes, match against the database, label them."""

    def __init__(
        self,
        embedder: FaceEmbedder,
        database: FaceDatabase,
        *,
        match_threshold: Optional[float] = None,
    ) -> None:
        self._embedder = embedder
        self._db = database
        self._threshold = match_threshold

    @property
    def database(self) -> FaceDatabase:
        return self._db

    def identify_faces(
        self,
        image_rgb: Any,
        face_boxes: Sequence[tuple[int, int, int, int]],
    ) -> list[IdentifiedFace]:
        """Embed + identify each (x1,y1,x2,y2) face box. Unmatched -> unknown."""
        results: list[IdentifiedFace] = []
        for box in face_boxes:
            x1, y1, x2, y2 = box
            emb = self._embedder.embed(image_rgb, box)
            if emb is None:
                match = FaceMatch(UNKNOWN_LABEL, 0.0, False)
            else:
                match = self._db.identify(emb, threshold=self._threshold)
            results.append(
                IdentifiedFace(
                    label=match.label,
                    score=match.score,
                    x=float((x1 + x2) / 2),
                    y=float((y1 + y2) / 2),
                    width=float(x2 - x1),
                    height=float(y2 - y1),
                )
            )
        return results

    def enroll_from_image(
        self,
        label: str,
        image_rgb: Any,
        box: tuple[int, int, int, int],
    ) -> bool:
        """Embed the face at ``box`` and store it under ``label``.

        Returns False if no embedding could be produced (no face / null
        embedder). Persisting to disk is the caller's responsibility.
        """
        emb = self._embedder.embed(image_rgb, box)
        if emb is None:
            return False
        self._db.enroll(label, emb)
        return True
