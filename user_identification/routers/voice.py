import aiosqlite
from fastapi import APIRouter, File, Form, Depends, Request, UploadFile

from database import get_db
from services.voice_service import VoiceService, validate_audio_bytes
from models.user import get_user
from schemas.voice import VoiceSearchResponse
from exceptions import VoiceTaskFailedError
from config import VOICE_MIN_SCORE

router = APIRouter(prefix="/api/voice", tags=["Voice"])


def _voice_service(request: Request) -> VoiceService:
    return request.app.state.voice_service


async def _validate_audio(audio: UploadFile) -> bytes:
    audio_bytes = await audio.read()
    validate_audio_bytes(audio_bytes)
    return audio_bytes


@router.post("/search", response_model=VoiceSearchResponse)
async def search_voice(
    request: Request,
    audio: UploadFile = File(...),
    top_k: int = Form(1),
    db: aiosqlite.Connection = Depends(get_db),
):
    audio_bytes = await _validate_audio(audio)
    voice_svc: VoiceService = _voice_service(request)

    try:
        result = await voice_svc.search(audio_bytes=audio_bytes, top_k=top_k)
    except VoiceTaskFailedError:
        return VoiceSearchResponse(results=[], score_list=[])

    score_list = result.get("scoreList", [])

    enriched = []
    for item in score_list:
        score = item.get("score", 0)
        if score < VOICE_MIN_SCORE:
            continue
        feature_id = item.get("featureId", "")
        user = await get_user(db, feature_id)
        if not user:
            continue
        enriched.append({
            "score": score,
            "feature_info": item.get("featureInfo", ""),
            "user_id": feature_id,
            "name": user.get("name", ""),
        })

    return VoiceSearchResponse(results=enriched, score_list=score_list)