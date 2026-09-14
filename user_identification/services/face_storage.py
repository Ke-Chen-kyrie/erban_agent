import base64
import aiofiles
import aiofiles.os
import logging
from pathlib import Path

from exceptions import FaceStorageError

logger = logging.getLogger(__name__)


class FaceStorage:
    """Local face image storage for display purposes only. Search uses Alibaba Cloud."""

    def __init__(self, base_dir: Path):
        self._base_dir = base_dir

    async def save(self, user_id: str, image_bytes: bytes, extension: str = "jpg") -> str:
        try:
            self._base_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{user_id}.{extension}"
            filepath = self._base_dir / filename
            async with aiofiles.open(filepath, "wb") as f:
                await f.write(image_bytes)
            return str(filepath)
        except OSError as e:
            logger.error("Failed to save face image for user %s: %s", user_id, e)
            raise FaceStorageError(detail=f"Failed to save face image: {e}") from e

    async def load_base64(self, user_id: str) -> str | None:
        for ext in ("jpg", "jpeg", "png", "bmp"):
            filepath = self._base_dir / f"{user_id}.{ext}"
            if filepath.is_file():
                async with aiofiles.open(filepath, "rb") as f:
                    data = await f.read()
                mime = "image/jpeg" if ext in ("jpg", "jpeg") else f"image/{ext}"
                encoded = base64.b64encode(data).decode("ascii")
                return f"data:{mime};base64,{encoded}"
        return None

    async def delete(self, user_id: str) -> bool:
        deleted = False
        for ext in ("jpg", "jpeg", "png", "bmp"):
            filepath = self._base_dir / f"{user_id}.{ext}"
            if filepath.is_file():
                await aiofiles.os.remove(filepath)
                deleted = True
        return deleted