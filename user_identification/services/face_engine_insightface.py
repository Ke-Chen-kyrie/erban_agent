"""InsightFace local face recognition engine (SCRFD detection + ArcFace recognition)."""

from __future__ import annotations

import logging
import threading

import numpy as np

logger = logging.getLogger(__name__)

MOUTH_INDICES_106 = list(range(52, 72))  # 20 mouth outer lip points


class InsightFaceEngine:
    """Lazy-loading InsightFace wrapper. Thread-safe for inference."""

    def __init__(self, model_dir: str = "", device: str = "cpu",
                 detection_size: tuple[int, int] = (640, 640)):
        self._model_dir = model_dir
        self._device = device
        self._detection_size = detection_size
        self._model = None
        self._lock = threading.Lock()

    def _get_model(self):
        if self._model is not None:
            return self._model
        with self._lock:
            if self._model is not None:
                return self._model
            import insightface
            providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if self._device == "cuda" else ["CPUExecutionProvider"]
            kwargs = {"name": "buffalo_l", "providers": providers}
            if self._model_dir:
                kwargs["root"] = self._model_dir
            try:
                model = insightface.app.FaceAnalysis(**kwargs)
                model.prepare(ctx_id=0 if self._device == "cuda" else -1, det_size=self._detection_size)
            except Exception as e:
                logger.error("Failed to initialize InsightFace: %s", e)
                raise RuntimeError(f"InsightFace initialization failed: {e}") from e
            self._model = model
            logger.info("InsightFace initialized: device=%s, det_size=%s", self._device, self._detection_size)
            return model

    def warmup(self) -> None:
        """Preload models — call at startup to avoid lazy loading on first request."""
        self._get_model()

    def _decode_image(self, image_bytes: bytes):
        import cv2
        if not image_bytes:
            raise ValueError("Empty image data")
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("Cannot decode image")
        return img

    def analyze(self, image_bytes: bytes, max_face_num: int = 10
                ) -> list[dict]:
        """Detect faces and extract embeddings. Returns list sorted by face area (desc)."""
        model = self._get_model()
        img = self._decode_image(image_bytes)
        faces = model.get(img, max_num=max_face_num)
        if not faces:
            return []
        faces = sorted(faces, key=lambda f: (
            (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1])
        ), reverse=True)
        result = []
        for face in faces:
            result.append({
                "bbox": [float(v) for v in face.bbox],
                "embedding": face.embedding.astype(np.float32),
                "det_score": float(face.det_score),
                "kps": face.kps.tolist() if hasattr(face, "kps") and face.kps is not None else None,
                "landmark_2d_106": face.landmark_2d_106.tolist() if hasattr(face, "landmark_2d_106") and face.landmark_2d_106 is not None else None,
                "pose": [float(face.pose[0]), float(face.pose[1]), float(face.pose[2])] if hasattr(face, "pose") and face.pose is not None else None,
                "gender": int(face.gender) if hasattr(face, "gender") and face.gender is not None else None,
                "age": int(face.age) if hasattr(face, "age") and face.age is not None else None,
            })
        return result

    def best_face(self, image_bytes: bytes) -> dict | None:
        faces = self.analyze(image_bytes, max_face_num=1)
        return faces[0] if faces else None

    @staticmethod
    def location(face: dict) -> dict:
        bbox = face.get("bbox")
        if not bbox or len(bbox) < 4:
            return {"x": 0, "y": 0, "width": 0, "height": 0}
        return {
            "x": int(bbox[0]),
            "y": int(bbox[1]),
            "width": int(bbox[2] - bbox[0]),
            "height": int(bbox[3] - bbox[1]),
        }

    @staticmethod
    def quality_score(face: dict) -> float:
        return float(face.get("det_score", 0.0)) * 100.0

    @staticmethod
    def normalized_embedding(face: dict) -> np.ndarray:
        emb = face.get("embedding")
        if emb is None:
            raise ValueError("Face dict missing embedding")
        emb = np.asarray(emb, dtype=np.float32)
        return emb / (np.linalg.norm(emb) + 1e-12)

    @staticmethod
    def mouth_points(face: dict) -> list[dict]:
        lm = face.get("landmark_2d_106")
        if not lm:
            return []
        return [{"x": float(lm[i][0]), "y": float(lm[i][1])} for i in MOUTH_INDICES_106 if i < len(lm)]