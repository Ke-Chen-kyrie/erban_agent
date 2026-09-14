import asyncio
import logging
from typing import Optional

from naviai_voiceprint import VoiceprintClient, VoiceprintConfig
from naviai_voiceprint.api.exceptions import (
    VoiceprintError,
    VoiceprintNotFoundError,
    VoiceprintServiceError,
)

from config import VOICEPRINT_ENGINE, VOICEPRINT_MODEL_PATH, VOICEPRINT_DB_PATH, VOICEPRINT_DEVICE
from exceptions import VoiceTaskFailedError

logger = logging.getLogger(__name__)

MAX_AUDIO_SIZE_BASE64 = 4 * 1024 * 1024  # 4MB after base64


def validate_audio_bytes(audio_bytes: bytes) -> None:
    """Validate audio size constraints. Raises InvalidAudioError on failure."""
    from exceptions import InvalidAudioError

    b64_len = len(audio_bytes) * 4 // 3 + 4
    if b64_len > MAX_AUDIO_SIZE_BASE64:
        raise InvalidAudioError(
            detail=f"Audio too large: {len(audio_bytes)} bytes. Max base64 size is {MAX_AUDIO_SIZE_BASE64}."
        )
    if len(audio_bytes) < 44:
        raise InvalidAudioError(detail="Audio too small to be valid.")

# Engine-specific module/class names for lazy loading
_ENGINE_SPECS = {
    "campp": {"package": "naviai_voiceprint_campp"},
    "eres2net": {"package": "naviai_voiceprint_eres2net"},
    "ecapa": {"package": "naviai_voiceprint_ecapa"},
}


class VoiceService:
    """Async wrapper around voiceprint SDK (xunfei cloud or edge ONNX models)."""

    def __init__(self, app_id: str = "", api_key: str = "", api_secret: str = "",
                 group_id: str = "default"):
        self._app_id = app_id
        self._api_key = api_key
        self._api_secret = api_secret
        self._group_id = group_id
        self._engine = VOICEPRINT_ENGINE
        self._client: Optional[VoiceprintClient] = None
        self._init_client()

    def _init_client(self) -> None:
        if self._engine == "xunfei":
            self._init_xunfei_client()
        elif self._engine in _ENGINE_SPECS:
            self._init_edge_client()
        else:
            raise ValueError(
                f"Unknown VOICEPRINT_ENGINE: {self._engine}. "
                f"Must be one of: xunfei, {', '.join(_ENGINE_SPECS)}"
            )

    # ── xunfei cloud ────────────────────────────────────────────

    def _init_xunfei_client(self) -> None:
        from naviai_voiceprint.core.engines.xunfei import XunfeiEngine

        _SERVICE_ID = "s1aa729d0"
        XunfeiEngine.API_PATH = f"/v1/private/{_SERVICE_ID}"

        _build_payload_orig = XunfeiEngine._build_payload

        def _build_payload_patched(self, func, *, audio_mp3=None, group_id="default",
                                   feature_id="", dst_feature_id="", top_k=5):
            # Strip WAV header (44 bytes) — Xunfei raw encoding expects pure PCM
            if audio_mp3 is not None and audio_mp3[:4] == b"RIFF":
                audio_mp3 = audio_mp3[44:]
            result = _build_payload_orig(self, func, audio_mp3=audio_mp3, group_id=group_id,
                                         feature_id=feature_id, dst_feature_id=dst_feature_id, top_k=top_k)
            if "s782b4996" in result.get("parameter", {}):
                result["parameter"][_SERVICE_ID] = result["parameter"].pop("s782b4996")
            if "payload" in result and "resource" in result["payload"]:
                result["payload"]["resource"]["encoding"] = "raw"
            return result

        XunfeiEngine._build_payload = _build_payload_patched

        config = VoiceprintConfig(
            engine="xunfei",
            credentials={
                "app_id": self._app_id,
                "api_key": self._api_key,
                "api_secret": self._api_secret,
            },
            group_id=self._group_id,
        )
        self._client = VoiceprintClient(config)
        self._client.__enter__()

    # ── edge ONNX ───────────────────────────────────────────────

    def _init_edge_client(self) -> None:
        if not VOICEPRINT_MODEL_PATH:
            raise ValueError(
                f"VOICEPRINT_MODEL_PATH is not set. "
                f"Edge engine '{self._engine}' requires a model path."
            )

        spec = _ENGINE_SPECS[self._engine]
        pkg = spec["package"]

        try:
            sdk = __import__(pkg, fromlist=["VoiceprintClient", "EdgeVoiceprintConfig"])
            EdgeClient = sdk.VoiceprintClient
            EdgeConfig = sdk.EdgeVoiceprintConfig
        except ImportError as e:
            raise ImportError(
                f"Edge SDK '{pkg}' is not installed. "
                f"Install it first, or set VOICEPRINT_ENGINE=xunfei."
            ) from e

        try:
            config = EdgeConfig(
                engine=self._engine,
                credentials={
                    "model_path": VOICEPRINT_MODEL_PATH,
                    "db_path": VOICEPRINT_DB_PATH,
                    "device": VOICEPRINT_DEVICE,
                },
                group_id=self._group_id,
            )
            self._client = EdgeClient(config)
            self._client.__enter__()
        except Exception as e:
            logger.error("Failed to initialize edge voiceprint client: %s", e)
            raise RuntimeError(f"Edge voiceprint initialization failed: {e}") from e

    # ── lifecycle ───────────────────────────────────────────────

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.__exit__(None, None, None)
            except Exception:
                pass
            self._client = None

    def _get_client(self) -> VoiceprintClient:
        if self._client is None:
            raise RuntimeError("VoiceService client is not initialized")
        return self._client

    @staticmethod
    def _map_exception(e: Exception, operation: str) -> Exception:
        if isinstance(e, VoiceprintError):
            logger.error("Voice SDK error in %s: category=%s code=%s http_status=%s message=%s",
                         operation, e.category.value if e.category else None, e.code, e.http_status, e.message)
            code = int(e.code) if e.code and str(e.code).isdigit() else None
            return VoiceTaskFailedError(detail=f"{operation}: {e.message}", api_code=code)
        logger.error("Unexpected error in %s: %s", operation, e)
        return VoiceTaskFailedError(detail=f"{operation}: {e}")

    async def _run_sync(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    # ── public API ───────────────────────────────────────────────

    async def create_group(self, group_name: str = "", group_info: str = "") -> dict:
        try:
            result = await self._run_sync(self._get_client().ensure_group, self._group_id)
            return {"success": result.success, "group_id": result.group_id, "message": result.message}
        except Exception as e:
            raise self._map_exception(e, "create_group")

    async def enroll(self, feature_id: str, audio_bytes: bytes, feature_info: str = "") -> dict:
        try:
            result = await self._run_sync(self._get_client().register, feature_id, audio_bytes, self._group_id)
            return {"success": result.success, "feature_id": result.user_id, "message": result.message}
        except Exception as e:
            raise self._map_exception(e, "enroll")

    async def search(self, audio_bytes: bytes, top_k: int = 1) -> dict:
        try:
            result = await self._run_sync(self._get_client().search, audio_bytes, self._group_id, top_k)
        except VoiceprintError as e:
            logger.warning("Voice search failed: %s", e)
            return {"scoreList": [], "error": str(e.message) if e.message else "voice search failed"}
        except Exception as e:
            logger.error("Voice search failed unexpectedly: %s", e)
            raise VoiceTaskFailedError(detail=f"Voice search failed: {e}") from e

        score_list = [
            {"score": m.score, "featureId": m.user_id, "featureInfo": ""}
            for m in result.matches
        ]
        return {"scoreList": score_list}

    async def query_features(self) -> list[dict] | None:
        try:
            result = await self._run_sync(self._get_client().list_features, self._group_id)
        except VoiceprintError as e:
            logger.warning("Query features failed: %s", e)
            return None
        except Exception as e:
            logger.warning("Query features failed unexpectedly: %s", e)
            return None

        return [{"featureId": uid} for uid in result.user_ids]

    async def delete_feature(self, feature_id: str) -> dict:
        try:
            await self._run_sync(self._get_client().delete, feature_id, self._group_id)
            return {"featureId": feature_id, "deleted": True}
        except VoiceprintServiceError as e:
            if e.code == "23006":
                logger.info("Feature %s already deleted on remote, treating as success", feature_id)
                return {"featureId": feature_id, "deleted": True}
            raise self._map_exception(e, "delete_feature")
        except VoiceprintNotFoundError:
            logger.info("Feature %s not found on remote, treating delete as success", feature_id)
            return {"featureId": feature_id, "deleted": True}
        except Exception as e:
            raise self._map_exception(e, "delete_feature")