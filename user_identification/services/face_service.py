import asyncio
import logging
import threading
from io import BytesIO
from pathlib import Path

import numpy as np

from config import FACE_ENGINE, FACE_MODEL_DIR, FACE_CHROMA_PATH, FACE_DEVICE, FACE_QUALITY_THRESHOLD
from exceptions import FaceComparisonError, FaceEnrollmentError, FaceDatabaseError

logger = logging.getLogger(__name__)


class FaceService:
    """Face recognition service — alibaba cloud or insightface local engine."""

    def __init__(self, ak_id: str = "", ak_secret: str = "", endpoint: str = "",
                 db_name: str = "default"):
        self._engine = FACE_ENGINE
        self._db_name = db_name
        self._ak_id = ak_id
        self._ak_secret = ak_secret
        self._endpoint = endpoint

        if self._engine == "alibaba":
            self._init_alibaba()
        elif self._engine == "insightface":
            self._init_insightface()
        else:
            raise ValueError(f"Unknown FACE_ENGINE: {self._engine}. Must be: alibaba, insightface")

        self._chroma_client = None
        self._init_storage()

    # ── alibaba cloud ────────────────────────────────────────────

    def _init_alibaba(self) -> None:
        from alibabacloud_facebody20191230.client import Client as FacebodyClient
        from alibabacloud_tea_openapi import models as open_api_models
        config = open_api_models.Config(
            access_key_id=self._ak_id,
            access_key_secret=self._ak_secret,
            endpoint=self._endpoint,
        )
        self._client = FacebodyClient(config)

    # ── insightface local ────────────────────────────────────────

    def _init_insightface(self) -> None:
        from services.face_engine_insightface import InsightFaceEngine
        self._face_engine = InsightFaceEngine(
            model_dir=FACE_MODEL_DIR,
            device=FACE_DEVICE,
        )

    # ── storage (insightface only) ────────────────────────────────

    def _init_storage(self) -> None:
        if self._engine != "insightface":
            return
        chroma_path = Path(FACE_CHROMA_PATH)
        chroma_path.mkdir(parents=True, exist_ok=True)
        import chromadb
        try:
            self._chroma_client = chromadb.PersistentClient(path=str(chroma_path))
        except Exception as e:
            logger.error("Failed to initialize ChromaDB at %s: %s", chroma_path, e)
            raise FaceDatabaseError(detail=f"ChromaDB initialization failed: {e}") from e
        self._chroma_lock = threading.RLock()

    def _get_collection(self):
        name = f"face-{self._db_name}"
        return self._chroma_client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
        )

    @staticmethod
    def _cosine_to_score(c: float) -> float:
        return float(max(0.0, min(1.0, (c + 1.0) / 2.0)))

    def _chroma_upsert(self, user_id: str, emb: np.ndarray) -> None:
        try:
            with self._chroma_lock:
                self._get_collection().upsert(
                    embeddings=[emb.astype(np.float32).tolist()],
                    ids=[user_id],
                )
        except Exception as e:
            logger.error("ChromaDB upsert failed for user %s: %s", user_id, e)
            raise FaceEnrollmentError(detail=f"Face enrollment failed: ChromaDB write error") from e

    def _chroma_query(self, emb: np.ndarray, top_k: int = 5
                      ) -> tuple[list[str], list[float]]:
        try:
            with self._chroma_lock:
                col = self._get_collection()
                n = col.count()
                if n == 0:
                    return [], []
                result = col.query(
                    query_embeddings=[emb.astype(np.float32).tolist()],
                    n_results=min(top_k, n),
                )
                ids = result["ids"][0] if result["ids"] else []
                distances = result["distances"][0] if result["distances"] else []
                scores = [1.0 - d for d in distances]
                return ids, scores
        except Exception as e:
            logger.error("ChromaDB query failed: %s", e)
            raise FaceComparisonError(detail=f"Face search failed: {e}") from e

    def _chroma_delete(self, user_id: str) -> None:
        with self._chroma_lock:
            try:
                self._get_collection().delete(ids=[user_id])
            except Exception:
                pass

    def _chroma_list_ids(self, limit: int = 200) -> list[str]:
        with self._chroma_lock:
            result = self._get_collection().get(limit=limit)
            return result["ids"] if result["ids"] else []

    # ── public API ─────────────────────────────────────────────────

    async def ensure_db(self) -> bool:
        if self._engine == "alibaba":
            try:
                await self.create_face_db(self._db_name)
                return True
            except FaceDatabaseError:
                return False
        else:
            return True

    async def warmup(self) -> None:
        """Preload models at startup — avoids lazy loading on first request."""
        if self._engine == "insightface":
            await asyncio.to_thread(self._face_engine.warmup)
            logger.info("InsightFace models preloaded")

    async def create_face_db(self, name: str) -> dict:
        if self._engine == "alibaba":
            return await self._alibaba_call("create_face_db", name)
        return {"request_id": "", "name": name, "created": True}

    async def add_face_entity(self, entity_id: str, labels: str = "",
                              db_name: str | None = None) -> dict:
        if self._engine == "alibaba":
            return await self._alibaba_call("add_face_entity", entity_id, labels, db_name)
        return {"request_id": "", "entity_id": entity_id}

    async def delete_face_entity(self, entity_id: str, db_name: str | None = None) -> dict:
        if self._engine == "alibaba":
            return await self._alibaba_call("delete_face_entity", entity_id, db_name)
        self._chroma_delete(entity_id)
        return {"request_id": "", "entity_id": entity_id, "deleted": True}

    async def list_entities(self, limit: int = 200, db_name: str | None = None
                            ) -> list[str] | None:
        if self._engine == "alibaba":
            return await self._alibaba_call("list_entities", limit, db_name)
        try:
            return self._chroma_list_ids(limit=limit)
        except Exception as e:
            logger.error("List entities failed: %s", e)
            return None

    async def add_face(
        self,
        entity_id: str,
        image_bytes: bytes,
        extra_data: str = "",
        quality_score_threshold: float = FACE_QUALITY_THRESHOLD,
        db_name: str | None = None,
    ) -> dict:
        if self._engine == "alibaba":
            return await self._alibaba_call("add_face", entity_id, image_bytes, extra_data,
                                            quality_score_threshold, db_name)
        try:
            face = await asyncio.to_thread(self._face_engine.best_face, image_bytes)
        except ValueError as e:
            raise FaceEnrollmentError(detail=f"Cannot decode image: {e}") from e
        if face is None:
            raise FaceEnrollmentError(detail="No face detected in image")
        score = self._face_engine.quality_score(face)
        if score < quality_score_threshold:
            raise FaceEnrollmentError(detail=f"Face quality too low: {score:.1f} < {quality_score_threshold}")
        emb = self._face_engine.normalized_embedding(face)
        self._chroma_upsert(entity_id, emb)
        return {
            "face_id": entity_id,
            "qualitie_score": score,
            "request_id": "",
        }

    async def search_face(
        self,
        image_bytes: bytes,
        limit: int = 5,
        max_face_num: int = 1,
        quality_score_threshold: float = FACE_QUALITY_THRESHOLD,
        db_name: str | None = None,
    ) -> dict:
        if self._engine == "alibaba":
            return await self._alibaba_call("search_face", image_bytes, limit, max_face_num,
                                            quality_score_threshold, db_name)
        try:
            faces = await asyncio.to_thread(self._face_engine.analyze, image_bytes, max_face_num)
        except ValueError as e:
            raise FaceComparisonError(detail=f"Cannot decode image: {e}") from e
        if not faces:
            return {"match_list": [], "request_id": ""}

        match_list = []
        for face in faces:
            loc = self._face_engine.location(face)
            qs = self._face_engine.quality_score(face)
            emb = self._face_engine.normalized_embedding(face)
            ids, scores = self._chroma_query(emb, limit)

            face_items = []
            for uid, score in zip(ids, scores):
                s = self._cosine_to_score(score)
                face_items.append({
                    "entity_id": uid,
                    "face_id": uid,
                    "score": s,
                    "confidence": s,
                    "extra_data": "",
                    "db_name": self._db_name,
                })

            match_list.append({
                "face_items": face_items,
                "location": loc,
                "qualitie_score": qs,
            })

        return {"match_list": match_list, "request_id": ""}

    async def detect_faces(self, image_bytes: bytes, max_face_num: int = 10) -> dict:
        if self._engine == "alibaba":
            return await self._alibaba_call("detect_faces", image_bytes, max_face_num)
        try:
            faces = await asyncio.to_thread(self._face_engine.analyze, image_bytes, max_face_num)
        except ValueError as e:
            raise FaceComparisonError(detail=f"Cannot decode image: {e}") from e
        result = []
        for face in faces:
            result.append({
                "location": self._face_engine.location(face),
                "mouth": self._face_engine.mouth_points(face),
            })
        return {"faces": result, "request_id": ""}

    async def analyze_faces(self, image_bytes: bytes, max_face_num: int = 10) -> list[dict]:
        if self._engine != "insightface":
            raise FaceComparisonError(detail="analyze_faces only available with insightface engine")
        return await asyncio.to_thread(self._face_engine.analyze, image_bytes, max_face_num)

    # ── alibaba cloud backend ──────────────────────────────────────

    async def _alibaba_call(self, method: str, *args, **kwargs):
        """Dispatch to Alibaba Cloud SDK methods."""
        from Tea.exceptions import TeaException

        if method == "create_face_db":
            from alibabacloud_facebody20191230 import models as facebody_models
            name = args[0]
            try:
                request = facebody_models.CreateFaceDbRequest(name=name)
                response = await asyncio.to_thread(self._client.create_face_db, request)
                return {"request_id": response.body.request_id, "name": name, "created": True}
            except TeaException as e:
                logger.warning("CreateFaceDb: %s (code=%s)", e.message, e.code)
                raise FaceDatabaseError(detail=f"Create face db failed: {e.message or 'unknown error'}") from e

        elif method == "add_face_entity":
            from alibabacloud_facebody20191230 import models as facebody_models
            entity_id, labels, db_name = args[0], args[1], args[2]
            try:
                request = facebody_models.AddFaceEntityRequest(
                    db_name=db_name or self._db_name, entity_id=entity_id, labels=labels)
                response = await asyncio.to_thread(self._client.add_face_entity, request)
                return {"request_id": response.body.request_id, "entity_id": entity_id}
            except TeaException as e:
                logger.error("AddFaceEntity error: code=%s, message=%s", e.code, e.message)
                raise FaceEnrollmentError(detail=f"Add face entity failed: {e.message or 'unknown error'}") from e

        elif method == "delete_face_entity":
            from alibabacloud_facebody20191230 import models as facebody_models
            entity_id, db_name = args[0], args[1]
            try:
                request = facebody_models.DeleteFaceEntityRequest(
                    db_name=db_name or self._db_name, entity_id=entity_id)
                response = await asyncio.to_thread(self._client.delete_face_entity, request)
                return {"request_id": response.body.request_id, "entity_id": entity_id, "deleted": True}
            except TeaException as e:
                logger.error("DeleteFaceEntity error: code=%s, message=%s", e.code, e.message)
                raise FaceDatabaseError(detail=f"Delete face entity failed: {e.message or 'unknown error'}") from e

        elif method == "list_entities":
            from alibabacloud_facebody20191230 import models as facebody_models
            limit, db_name = args[0], args[1]
            try:
                request = facebody_models.ListFaceEntitiesRequest(
                    db_name=db_name or self._db_name, limit=limit, offset=0)
                response = await asyncio.to_thread(self._client.list_face_entities, request)
                entities = response.body.data.entities or []
                return [e.entity_id for e in entities]
            except Exception as e:
                logger.error("ListFaceEntities error: %s", e)
                return None

        elif method == "add_face":
            from alibabacloud_facebody20191230 import models as facebody_models
            from alibabacloud_tea_util import models as util_models
            entity_id, image_bytes, extra_data, quality_score_threshold, db_name = args
            try:
                request = facebody_models.AddFaceAdvanceRequest(
                    db_name=db_name or self._db_name, entity_id=entity_id,
                    image_url_object=BytesIO(image_bytes), extra_data=extra_data,
                    quality_score_threshold=quality_score_threshold)
                response = await asyncio.to_thread(
                    self._client.add_face_advance, request, util_models.RuntimeOptions())
                data = response.body.data
                return {
                    "face_id": data.face_id or "",
                    "qualitie_score": float(data.qualitie_score) if data.qualitie_score else 0.0,
                    "request_id": response.body.request_id,
                }
            except TeaException as e:
                logger.error("AddFace error: code=%s, message=%s", e.code, e.message)
                raise FaceEnrollmentError(detail=f"Add face failed: {e.message or 'unknown error'}") from e

        elif method == "search_face":
            from alibabacloud_facebody20191230 import models as facebody_models
            from alibabacloud_tea_util import models as util_models
            image_bytes, limit, max_face_num, quality_score_threshold, db_name = args
            try:
                request = facebody_models.SearchFaceAdvanceRequest(
                    db_name=db_name or self._db_name, image_url_object=BytesIO(image_bytes),
                    limit=limit, max_face_num=max_face_num,
                    quality_score_threshold=quality_score_threshold)
                response = await asyncio.to_thread(
                    self._client.search_face_advance, request, util_models.RuntimeOptions())
            except TeaException as e:
                msg = e.message or ""
                if "not found face" in msg:
                    return {"match_list": [], "request_id": ""}
                logger.warning("SearchFace: %s (code=%s)", msg, e.code)
                raise FaceComparisonError(detail=f"Face search failed: {msg}") from e

            match_list = []
            for item in (response.body.data.match_list or []):
                face_items = []
                for fi in (item.face_items or []):
                    face_items.append({
                        "entity_id": fi.entity_id or "", "face_id": fi.face_id or "",
                        "score": float(fi.score) if fi.score else 0.0,
                        "confidence": float(fi.confidence) if fi.confidence else 0.0,
                        "extra_data": fi.extra_data or "", "db_name": fi.db_name or "",
                    })
                location = None
                if item.location:
                    location = {
                        "width": item.location.width, "height": item.location.height,
                        "x": item.location.x, "y": item.location.y,
                    }
                match_list.append({
                    "face_items": face_items, "location": location,
                    "qualitie_score": float(item.qualitie_score) if item.qualitie_score else 0.0,
                })
            return {"match_list": match_list, "request_id": response.body.request_id}

        elif method == "detect_faces":
            from alibabacloud_facebody20191230 import models as facebody_models
            from alibabacloud_tea_util import models as util_models
            image_bytes, max_face_num = args[0], args[1]
            try:
                request = facebody_models.DetectFaceAdvanceRequest(
                    image_urlobject=BytesIO(image_bytes), landmark=True,
                    max_face_number=max_face_num)
                response = await asyncio.to_thread(
                    self._client.detect_face_advance, request, util_models.RuntimeOptions())
            except TeaException as e:
                logger.warning("DetectFace: %s (code=%s)", e.message, e.code)
                raise FaceComparisonError(detail=f"Face detection failed: {e.message or 'unknown error'}") from e

            data = response.body.data
            face_count = data.face_count or 0
            if face_count == 0 or not data.face_rectangles or not data.landmarks:
                return {"faces": [], "request_id": response.body.request_id}

            faces = []
            landmark_count = data.landmark_count or 105
            for i in range(face_count):
                rect_offset = i * 4
                rect = data.face_rectangles[rect_offset:rect_offset + 4]
                location = {"x": int(rect[0]), "y": int(rect[1]),
                            "width": int(rect[2]), "height": int(rect[3])}
                points_per_face = landmark_count * 2
                lm_offset = i * points_per_face
                lm_end = lm_offset + points_per_face
                lm_flat = data.landmarks[lm_offset:lm_end]
                landmarks = []
                for j in range(0, len(lm_flat), 2):
                    landmarks.append({"x": lm_flat[j], "y": lm_flat[j + 1]})
                mouth = landmarks[62:96]
                faces.append({"location": location, "mouth": mouth})
            return {"faces": faces, "request_id": response.body.request_id}

        raise ValueError(f"Unknown method: {method}")