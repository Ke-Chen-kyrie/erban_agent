"""声纹识别模块 —— 从 main.py 提取的 SpeakerIdentifier。"""

from identity import IdentificationClient
from config import VOICEPRINT_THRESHOLD
from logging_config import get_logger

logger = get_logger(__name__)


class SpeakerIdentifier:
    """声纹识别与用户信息查询。"""

    def __init__(self, ident_client: IdentificationClient):
        self._client = ident_client
        self.possible_speakers: list[dict] = []

    async def refresh_speakers(self):
        """从识别服务获取所有已注册用户作为可能说话人列表。"""
        try:
            result = await self._client.list_users_async()
            self.possible_speakers = result.get("users", [])
            names = [u.get("name", u.get("user_id", "")) for u in self.possible_speakers]
            logger.info(f"[ident] 已加载 {len(self.possible_speakers)} 个可能说话人: {', '.join(names) if names else '(无)'}")
        except Exception as e:
            logger.error(f"[ident] 加载可能说话人失败: {e}")

    async def identify(self, wav_bytes: bytes) -> str:
        """声纹识别，返回 user_id 或 'unknown'。"""
        if not wav_bytes:
            return "unknown"
        try:
            logger.info(f"[ident] WAV bytes: {len(wav_bytes)} bytes")
            result = await self._client.voice_search_bytes_async(wav_bytes, top_k=1)
            logger.debug(f"[ident] API 原始响应: {result}")
            items = result.get("results", [])
            if items:
                best = items[0]
                uid = best.get("user_id", "")
                score = best.get("score", 0)
                if uid and score >= VOICEPRINT_THRESHOLD:
                    logger.info(f"[ident] 识别成功: user_id={uid} score={score:.4f}")
                    return uid
                else:
                    logger.info(f"[ident] 声纹不匹配: user_id={uid} score={score:.4f} < threshold={VOICEPRINT_THRESHOLD}")
        except Exception as e:
            logger.error(f"[ident] 声纹识别失败: {e}")
        return "unknown"

    async def get_user_info(self, user_id: str) -> tuple[str, str, str]:
        """查询用户名、角色和描述，未知用户返回 ('路人', '', '')。"""
        if user_id == "unknown":
            return "路人", "", ""
        if user_id == "system":
            return "系统", "", ""
        try:
            info = await self._client.get_user_info_async(user_id)
            name = info.get("name", "")
            role = info.get("role", "")
            description = info.get("description", "")
            logger.info(f"[ident] 用户名: {name}, 角色: {role}, 描述: {description}")
            return name if name else "路人", role, description
        except Exception as e:
            logger.error(f"[ident] 获取用户信息失败: {e}")
            return "路人", "", ""
