import asyncio
import aiosqlite
import logging
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile

from database import get_db
from services.face_service import FaceService
from services.voice_service import VoiceService
from services.face_storage import FaceStorage
from models.user import get_user, delete_user, get_all_users
from schemas.user import UserFaceResponse, UserRegisterResponse, UserDeleteResponse, UserListResponse, UserInfoResponse
from exceptions import (
    FaceComparisonError,
    FaceEnrollmentError,
    FaceStorageError,
    VoiceTaskFailedError,
)
from services.voice_service import validate_audio_bytes

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/user", tags=["User"])

ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/bmp"}

_register_locks: dict[str, asyncio.Lock] = {}
_register_locks_guard = asyncio.Lock()


async def _get_register_lock(user_id: str) -> asyncio.Lock:
    async with _register_locks_guard:
        if user_id not in _register_locks:
            _register_locks[user_id] = asyncio.Lock()
        return _register_locks[user_id]


def _face_service(request: Request) -> FaceService:
    return request.app.state.face_service


def _voice_service(request: Request) -> VoiceService:
    return request.app.state.voice_service


@router.post("/register", response_model=UserRegisterResponse)
async def register_user(
    request: Request,
    user_id: str = Form(...),
    name: str = Form(...),
    role: str = Form(""),
    description: str = Form(""),
    face_image: UploadFile = File(...),
    voice_audio: UploadFile = File(...),
    db: aiosqlite.Connection = Depends(get_db),
):
    if face_image.content_type not in ALLOWED_IMAGE_TYPES:
        raise FaceComparisonError(
            detail=f"Unsupported image type: {face_image.content_type}. Use JPEG, PNG, or BMP."
        )

    audio_bytes = await voice_audio.read()
    validate_audio_bytes(audio_bytes)

    image_bytes = await face_image.read()
    face_svc: FaceService = _face_service(request)
    voice_svc: VoiceService = _voice_service(request)
    face_storage: FaceStorage = request.app.state.face_storage
    ext = face_image.filename.split(".")[-1] if face_image.filename and "." in face_image.filename else "jpg"

    lock = await _get_register_lock(user_id)
    async with lock:
        existing = await get_user(db, user_id)

        # ── face enrollment ──
        face_id = None
        await face_svc.add_face_entity(entity_id=user_id, labels=name)

        try:
            result = await face_svc.add_face(entity_id=user_id, image_bytes=image_bytes, extra_data=f"name:{name}")
            face_id = result.get("face_id")
            await face_storage.save(user_id, image_bytes, ext)
        except (FaceEnrollmentError, FaceStorageError) as e:
            logger.error("Face enrollment failed for user %s: %s", user_id, e)
            try:
                await face_svc.delete_face_entity(entity_id=user_id)
            except Exception:
                pass
            raise FaceEnrollmentError(detail=f"Face registration failed: {e.detail}")

        # ── voice enrollment ──
        try:
            await voice_svc.enroll(feature_id=user_id, audio_bytes=audio_bytes, feature_info=f"user:{user_id}")
        except VoiceTaskFailedError as e:
            logger.error("Voice enrollment failed for user %s: %s", user_id, e)
            try:
                await face_svc.delete_face_entity(entity_id=user_id)
            except Exception:
                pass
            try:
                await face_storage.delete(user_id)
            except Exception:
                pass
            raise VoiceTaskFailedError(detail=f"Voice registration failed: {e.detail}")

        # ── both succeeded, persist to local DB ──
        try:
            if existing:
                await db.execute(
                    "UPDATE users SET name = ?, role = ?, description = ?, updated_at = datetime('now') WHERE user_id = ?",
                    (name, role, description, user_id),
                )
            else:
                await db.execute(
                    "INSERT INTO users (user_id, name, role, description) VALUES (?, ?, ?, ?)",
                    (user_id, name, role, description),
                )
            await db.commit()
        except Exception as e:
            logger.error("DB commit failed for user %s, rolling back external resources: %s", user_id, e)
            try:
                await face_svc.delete_face_entity(entity_id=user_id)
            except Exception:
                pass
            try:
                await face_storage.delete(user_id)
            except Exception:
                pass
            try:
                await voice_svc.delete_feature(feature_id=user_id)
            except Exception:
                pass
            raise FaceEnrollmentError(detail=f"Registration failed: database error") from e

        return UserRegisterResponse(
            user_id=user_id,
            name=name,
            role=role,
            description=description,
            face_id=face_id,
        )


@router.get("/{user_id}/face", response_model=UserFaceResponse)
async def get_user_face(
    request: Request,
    user_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    user = await get_user(db, user_id)
    if not user:
        return UserFaceResponse(user_id=user_id, face_image=None)

    face_storage: FaceStorage = request.app.state.face_storage
    face_image = await face_storage.load_base64(user_id)

    return UserFaceResponse(
        user_id=user_id,
        name=user.get("name", ""),
        face_image=face_image,
    )


@router.delete("/{user_id}", response_model=UserDeleteResponse)
async def delete_user_api(
    request: Request,
    user_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    user = await get_user(db, user_id)
    if not user:
        return UserDeleteResponse(user_id=user_id, deleted=False, message="User not found")

    face_svc: FaceService = _face_service(request)
    voice_svc: VoiceService = _voice_service(request)
    face_storage: FaceStorage = request.app.state.face_storage

    # Delete DB row first — if this fails, cloud resources are intact
    await delete_user(db, user_id)

    # Then clean up external resources
    failed = []

    try:
        await face_svc.delete_face_entity(entity_id=user_id)
    except Exception as e:
        failed.append(f"face: {e}")

    try:
        await face_storage.delete(user_id)
    except Exception as e:
        failed.append(f"face_storage: {e}")

    try:
        await voice_svc.delete_feature(feature_id=user_id)
    except Exception as e:
        failed.append(f"voice: {e}")

    if failed:
        return UserDeleteResponse(
            user_id=user_id,
            deleted=False,
            message=f"Partial failure: {'; '.join(failed)}",
        )

    return UserDeleteResponse(user_id=user_id, deleted=True, message="User deleted successfully")


@router.get("/list", response_model=UserListResponse)
async def list_users(db: aiosqlite.Connection = Depends(get_db)):
    users = await get_all_users(db)
    return UserListResponse(total=len(users), users=users)


@router.get("/{user_id}", response_model=UserInfoResponse)
async def get_user_info(
    user_id: str,
    db: aiosqlite.Connection = Depends(get_db),
):
    user = await get_user(db, user_id)
    if not user:
        return UserInfoResponse(user_id=user_id, exists=False)
    return UserInfoResponse(
        user_id=user_id,
        name=user.get("name", ""),
        role=user.get("role", ""),
        description=user.get("description", ""),
        created_at=user.get("created_at", ""),
        exists=True,
    )