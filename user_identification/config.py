import logging
import os
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


def _resolve_device(env_val: str) -> str:
    """Resolve device: auto-detect CUDA, fallback to CPU. Explicit 'cuda' also validated."""
    val = env_val.strip().lower()
    if val in ("cuda", "auto"):
        try:
            import onnxruntime
            if "CUDAExecutionProvider" in onnxruntime.get_available_providers():
                return "cuda"
        except Exception:
            pass
        if val == "cuda":
            logger.warning("CUDA requested but not available, falling back to CPU")
    return "cpu"


# iFlytek voiceprint credentials
APP_ID = os.getenv("IFLYTEK_APP_ID", "")
API_KEY = os.getenv("IFLYTEK_API_KEY", "")
API_SECRET = os.getenv("IFLYTEK_API_SECRET", "")

# Alibaba Cloud AK for face service
AK_ID = os.getenv("ALIBABA_AK_ID", "")
AK_SECRET = os.getenv("ALIBABA_AK_SECRET", "")

# Cloud database/group names — change per deployment to isolate tenants
FACE_DB_NAME = os.getenv("FACE_DB_NAME", "default")
VOICE_GROUP_ID = os.getenv("VOICE_GROUP_ID", "default")

# Voiceprint engine — xunfei | campp | eres2net | ecapa
VOICEPRINT_ENGINE = os.getenv("VOICEPRINT_ENGINE", "xunfei")
VOICEPRINT_MODEL_PATH = os.getenv("VOICEPRINT_MODEL_PATH", "")
VOICEPRINT_DB_PATH = os.getenv("VOICEPRINT_DB_PATH", "data/chroma_db/voice")
VOICEPRINT_DEVICE = _resolve_device(os.getenv("VOICEPRINT_DEVICE", "auto"))

# Face engine — alibaba | insightface
FACE_ENGINE = os.getenv("FACE_ENGINE", "alibaba")
FACE_MODEL_DIR = os.getenv("FACE_MODEL_DIR", "")
FACE_CHROMA_PATH = os.getenv("FACE_CHROMA_PATH", "data/chroma_db/face")
FACE_DEVICE = _resolve_device(os.getenv("FACE_DEVICE", "auto"))

# Score threshold for search — scores below this are filtered out
def _parse_float_config(name: str, default: float) -> float:
    raw = os.getenv(name, str(default))
    try:
        return float(raw)
    except (ValueError, TypeError):
        logger.error("%s must be a number, got %r — using default %s", name, raw, default)
        return default

VOICE_MIN_SCORE = _parse_float_config("VOICE_MIN_SCORE", 0.5)
FACE_MIN_SCORE = _parse_float_config("FACE_MIN_SCORE", 0.5)

# Face thresholds
FACE_VERIFY_THRESHOLD = _parse_float_config("FACE_VERIFY_THRESHOLD", 0.5)
FACE_QUALITY_THRESHOLD = _parse_float_config("FACE_QUALITY_THRESHOLD", 30.0)