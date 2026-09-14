import asyncio
import aiosqlite
import logging
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from services.face_service import FaceService
from config import FACE_VERIFY_THRESHOLD, FACE_MIN_SCORE
from schemas.face import (
    FaceSearchResponse,
    FaceSearchItem,
    FaceVerifyResponse,
    FaceDetectResponse,
    FaceDetectItem,
    FaceLocation,
    LandmarkPoint,
    FaceAnalyzeItem,
    FaceAnalyzeResponse,
)
from exceptions import FaceComparisonError
from database import get_db
from models.user import get_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/face", tags=["Face"])

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/bmp"}


def _face_service(request: Request) -> FaceService:
    return request.app.state.face_service


@router.post("/search", response_model=FaceSearchResponse)
async def search_face(
    request: Request,
    image: UploadFile = File(...),
    top_k: int = Form(5),
):
    if image.content_type not in ALLOWED_IMAGE_TYPES:
        raise FaceComparisonError(
            detail=f"Unsupported image type: {image.content_type}. Use JPEG, PNG, or BMP."
        )

    image_bytes = await image.read()
    face_svc: FaceService = _face_service(request)

    result = await face_svc.search_face(
        image_bytes=image_bytes,
        limit=top_k,
        max_face_num=1,
    )

    items = []
    for match in result["match_list"]:
        for fi in match["face_items"]:
            if fi["score"] < FACE_MIN_SCORE:
                continue
            items.append(FaceSearchItem(
                entity_id=fi["entity_id"],
                face_id=fi["face_id"],
                score=fi["score"],
                confidence=fi["confidence"],
                db_name=fi["db_name"],
                extra_data=fi["extra_data"],
                qualitie_score=match["qualitie_score"],
            ))

    return FaceSearchResponse(results=items, request_id=result["request_id"])


@router.post("/verify", response_model=FaceVerifyResponse)
async def verify_face(
    request: Request,
    user_id: str = Form(...),
    image: UploadFile = File(...),
):
    if image.content_type not in ALLOWED_IMAGE_TYPES:
        raise FaceComparisonError(
            detail=f"Unsupported image type: {image.content_type}. Use JPEG, PNG, or BMP."
        )

    image_bytes = await image.read()
    face_svc: FaceService = _face_service(request)

    result = await face_svc.search_face(
        image_bytes=image_bytes,
        limit=5,
        max_face_num=1,
    )

    matched_item = None
    for match in result["match_list"]:
        for fi in match["face_items"]:
            if fi["entity_id"] == user_id:
                matched_item = fi
                break

    threshold = FACE_VERIFY_THRESHOLD
    if matched_item:
        return FaceVerifyResponse(
            user_id=user_id,
            entity_id=matched_item["entity_id"],
            face_id=matched_item["face_id"],
            score=matched_item["score"],
            confidence=matched_item["confidence"],
            match=matched_item["score"] >= threshold,
            threshold=threshold,
            db_name=matched_item["db_name"],
            request_id=result["request_id"],
        )

    return FaceVerifyResponse(
        user_id=user_id,
        match=False,
        threshold=threshold,
        request_id=result["request_id"],
    )


@router.post("/detect", response_model=FaceDetectResponse)
async def detect_faces(
    request: Request,
    image: UploadFile = File(...),
    max_face_num: int = Form(10),
    db: aiosqlite.Connection = Depends(get_db),
):
    if max_face_num < 1 or max_face_num > 100:
        raise FaceComparisonError(
            detail=f"max_face_num must be between 1 and 100, got {max_face_num}"
        )
    if image.content_type not in ALLOWED_IMAGE_TYPES:
        raise FaceComparisonError(
            detail=f"Unsupported image type: {image.content_type}. Use JPEG, PNG, or BMP."
        )

    image_bytes = await image.read()
    face_svc: FaceService = _face_service(request)

    # 并行调用：SearchFace（匹配库）+ DetectFace（关键点）
    search_task = face_svc.search_face(
        image_bytes=image_bytes,
        limit=1,
        max_face_num=max_face_num,
    )
    detect_task = face_svc.detect_faces(
        image_bytes=image_bytes,
        max_face_num=max_face_num,
    )

    try:
        search_result, detect_result = await asyncio.gather(search_task, detect_task)
    except Exception:
        # Try to get partial results if one side failed
        search_result = {"match_list": [], "request_id": ""}
        detect_result = {"faces": [], "request_id": ""}
        try:
            search_result = await search_task
        except Exception:
            pass
        try:
            detect_result = await detect_task
        except Exception:
            pass
        if not search_result["match_list"] and not detect_result["faces"]:
            raise FaceComparisonError(detail="Face detection failed")

    # 按人脸中心点匹配两个结果
    def _center(loc):
        return (loc["x"] + loc["width"] / 2, loc["y"] + loc["height"] / 2)

    search_faces = search_result["match_list"]
    detect_faces_list = detect_result["faces"]

    def _closest_search(loc):
        if not search_faces:
            return None
        cx, cy = _center(loc)
        best = min(search_faces, key=lambda m: (
            (cx - _center(m["location"])[0]) ** 2 + (cy - _center(m["location"])[1]) ** 2
        ) if m["location"] else float("inf"))
        best_cx, best_cy = _center(best["location"]) if best["location"] else (0, 0)
        dist = ((cx - best_cx) ** 2 + (cy - best_cy) ** 2) ** 0.5
        return best if dist < max(loc["width"], loc["height"]) * 2 else None

    faces = []
    unknown_idx = 0

    for df in detect_faces_list:
        loc = df["location"]
        location = FaceLocation(
            x=loc["x"],
            y=loc["y"],
            width=loc["width"],
            height=loc["height"],
        )

        mouth = [LandmarkPoint(x=p["x"], y=p["y"]) for p in df["mouth"]]

        matched = _closest_search(loc)
        if matched and matched["face_items"] and matched["face_items"][0]["score"] >= FACE_MIN_SCORE:
            top = matched["face_items"][0]
            entity_id = top["entity_id"]
            user = await get_user(db, entity_id)
            faces.append(FaceDetectItem(
                user_id=entity_id,
                name=user["name"] if user else "",
                score=top["score"],
                matched=True,
                location=location,
                mouth=mouth,
            ))
        else:
            unknown_idx += 1
            faces.append(FaceDetectItem(
                user_id=f"unknown_{unknown_idx:03d}",
                name="",
                score=0.0,
                matched=False,
                location=location,
                mouth=mouth,
            ))

    return FaceDetectResponse(
        faces=faces,
        total_faces=len(faces),
        matched_count=sum(1 for f in faces if f.matched),
        unknown_count=sum(1 for f in faces if not f.matched),
        request_id=search_result["request_id"],
    )


@router.post("/analyze", response_model=FaceAnalyzeResponse)
async def analyze_faces(
    request: Request,
    image: UploadFile = File(...),
    max_face_num: int = Form(10),
):
    if image.content_type not in ALLOWED_IMAGE_TYPES:
        raise FaceComparisonError(
            detail=f"Unsupported image type: {image.content_type}. Use JPEG, PNG, or BMP."
        )

    image_bytes = await image.read()
    face_svc: FaceService = _face_service(request)

    faces = await face_svc.analyze_faces(image_bytes=image_bytes, max_face_num=max_face_num)

    items = [
        FaceAnalyzeItem(
            bbox=f["bbox"],
            det_score=f["det_score"],
            kps=f.get("kps"),
            pose=f.get("pose"),
        )
        for f in faces
    ]

    return FaceAnalyzeResponse(faces=items)