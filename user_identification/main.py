from contextlib import asynccontextmanager
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from database import init_db
from config import API_KEY, API_SECRET, APP_ID, AK_ID, AK_SECRET, FACE_DB_NAME, VOICE_GROUP_ID, VOICEPRINT_ENGINE, FACE_ENGINE, VOICEPRINT_DEVICE, FACE_DEVICE
from settings import FACE_ENDPOINT, STORAGE_DIR, DATABASE_PATH
from services.face_service import FaceService
from services.voice_service import VoiceService
from services.face_storage import FaceStorage
from exceptions import AppBaseError
from routers import face, voice, user

logger = logging.getLogger(__name__)


async def _get_local_user_ids() -> set[str]:
    """Return the set of user_id from the local SQLite database."""
    import aiosqlite

    async with aiosqlite.connect(str(DATABASE_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT user_id FROM users")
        rows = await cursor.fetchall()
        return {row["user_id"] for row in rows}


async def _cleanup_orphans(list_fn, delete_fn, id_extractor, label: str) -> list[str]:
    """Remove cloud resources that have no matching local user."""
    removed = []
    try:
        items = await list_fn()
    except Exception:
        logger.warning("Cannot query %s — skipping orphan cleanup", label)
        return removed

    if not items:
        if items is None:
            logger.warning("Query %s returned None — skipping orphan cleanup", label)
        return removed

    local_ids = await _get_local_user_ids()

    for item in items:
        eid = id_extractor(item)
        if eid and eid not in local_ids:
            try:
                await delete_fn(eid)
                logger.info("Cleaned up orphan %s: %s", label, eid)
                removed.append(eid)
            except Exception:
                pass
    return removed


async def _cleanup_orphan_voice_features(voice_service: VoiceService) -> list[str]:
    return await _cleanup_orphans(
        list_fn=voice_service.query_features,
        delete_fn=lambda fid: voice_service.delete_feature(feature_id=fid),
        id_extractor=lambda f: f.get("featureId", ""),
        label="voice feature",
    )


async def _cleanup_orphan_face_entities(face_service: FaceService) -> list[str]:
    return await _cleanup_orphans(
        list_fn=face_service.list_entities,
        delete_fn=lambda eid: face_service.delete_face_entity(entity_id=eid),
        id_extractor=lambda eid: eid,
        label="face entity",
    )


async def _verify_user_consistency(
    face_service: FaceService,
    voice_service: VoiceService,
) -> list[str]:
    """Verify each local user has matching cloud face entity, voice feature, and local face image.

    If any user is missing any of the four records, delete from ALL places
    (local DB, local face image, cloud face entity, cloud voice feature) to maintain strict consistency.
    """
    import aiosqlite

    cleaned = []
    async with aiosqlite.connect(str(DATABASE_PATH)) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT user_id, name FROM users")
        rows = await cursor.fetchall()
        local_users = [(row["user_id"], row["name"]) for row in rows]

    if not local_users:
        return cleaned

    cloud_entities = None
    cloud_features = None

    try:
        cloud_entities = await face_service.list_entities()
    except Exception:
        logger.warning("Cannot query face entities — skipping consistency check")

    try:
        cloud_features = await voice_service.query_features()
    except Exception:
        logger.warning("Cannot query voice features — skipping consistency check")

    if cloud_entities is None or cloud_features is None:
        logger.warning("Skipping user consistency check due to cloud API unavailability")
        return cleaned

    cloud_entity_ids = set(cloud_entities)
    cloud_feature_ids = {f.get("featureId", "") for f in cloud_features}
    storage = FaceStorage(STORAGE_DIR)

    inconsistent_ids = []
    for user_id, name in local_users:
        has_face_entity = user_id in cloud_entity_ids
        has_voice = user_id in cloud_feature_ids
        has_local_image = await storage.load_base64(user_id) is not None

        if has_face_entity and has_voice and has_local_image:
            continue

        logger.warning(
            "User %s (%s) inconsistent: face_entity=%s voice=%s local_image=%s — cleaning up all",
            user_id, name, has_face_entity, has_voice, has_local_image,
        )

        if has_face_entity:
            try:
                await face_service.delete_face_entity(entity_id=user_id)
            except Exception:
                pass
        if has_voice:
            try:
                await voice_service.delete_feature(feature_id=user_id)
            except Exception:
                pass

        try:
            await storage.delete(user_id)
        except Exception:
            pass

        inconsistent_ids.append(user_id)

    if inconsistent_ids:
        async with aiosqlite.connect(str(DATABASE_PATH)) as db:
            for uid in inconsistent_ids:
                await db.execute("DELETE FROM users WHERE user_id = ?", (uid,))
            await db.commit()

    return inconsistent_ids


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    app.state.face_service = FaceService(AK_ID, AK_SECRET, FACE_ENDPOINT, db_name=FACE_DB_NAME)
    app.state.voice_service = VoiceService(APP_ID, API_KEY, API_SECRET, group_id=VOICE_GROUP_ID)
    app.state.face_storage = FaceStorage(STORAGE_DIR)

    logger.info("Voiceprint engine: %s, device: %s", VOICEPRINT_ENGINE, VOICEPRINT_DEVICE)
    logger.info("Face engine: %s, device: %s", FACE_ENGINE, FACE_DEVICE)

    # ensure voiceprint group exists
    try:
        await app.state.voice_service.create_group(group_name="default", group_info="default group")
    except Exception:
        pass  # group may already exist

    # ensure face database exists and preload models
    await app.state.face_service.ensure_db()
    await app.state.face_service.warmup()

    # verify local users have matching cloud records; delete inconsistent ones
    await _verify_user_consistency(app.state.face_service, app.state.voice_service)

    # clean up orphan cloud data (no local user)
    await _cleanup_orphan_voice_features(app.state.voice_service)
    await _cleanup_orphan_face_entities(app.state.face_service)

    yield

    # cleanup
    try:
        app.state.voice_service.close()
    except Exception:
        pass


app = FastAPI(
    title="User Identification Service",
    description="Face and voice biometric authentication service with pluggable engines (Alibaba Cloud / InsightFace + iFlytek / CAM++ / ERes2Net / ECAPA)",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(AppBaseError)
async def app_exception_handler(request: Request, exc: AppBaseError):
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error_code": exc.error_code,
            "detail": exc.detail,
            "status_code": exc.status_code,
        },
    )


app.include_router(face.router)
app.include_router(voice.router)
app.include_router(user.router)


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "user-identification"}


@app.post("/api/admin/sync")
async def sync_orphan_cleanup(request: Request):
    """Manually trigger cleanup of cloud voice/face data with no local user."""
    voice_svc: VoiceService = request.app.state.voice_service
    face_svc: FaceService = request.app.state.face_service

    voice_result = await _cleanup_orphan_voice_features(voice_svc)
    face_result = await _cleanup_orphan_face_entities(face_svc)

    return {
        "status": "ok",
        "voice_cleanup": voice_result,
        "face_cleanup": face_result,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8001, reload=False)