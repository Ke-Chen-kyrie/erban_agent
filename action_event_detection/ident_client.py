"""SDK client for the User Identification Service API."""

import asyncio
from pathlib import Path
from typing import Any

import httpx


class IdentificationClient:
    """SDK client for the User Identification Service API."""

    def __init__(self, base_url: str = "http://localhost:8001"):
        self.base_url = base_url.rstrip("/")

    # ── sync API ──

    def health(self) -> dict[str, Any]:
        return asyncio.run(self.health_async())

    def register(
        self,
        user_id: str,
        name: str,
        face_path: str | Path,
        audio_path: str | Path,
        role: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        return asyncio.run(self.register_async(user_id, name, face_path, audio_path, role, description))

    def face_search(self, image_path: str | Path, top_k: int = 5) -> dict[str, Any]:
        return asyncio.run(self.face_search_async(image_path, top_k))

    def face_verify(self, user_id: str, image_path: str | Path) -> dict[str, Any]:
        return asyncio.run(self.face_verify_async(user_id, image_path))

    def face_detect(self, image_path: str | Path, max_face_num: int = 10) -> dict[str, Any]:
        return asyncio.run(self.face_detect_async(image_path, max_face_num))

    def face_detect_bytes(self, image_bytes: bytes, filename: str = "frame.jpg", max_face_num: int = 10) -> dict[str, Any]:
        mime = _guess_mime(Path(filename))
        with httpx.Client(timeout=30) as client:
            r = client.post(
                f"{self.base_url}/api/face/detect",
                data={"max_face_num": str(max_face_num)},
                files={"image": (filename, image_bytes, mime)},
            )
            self._check(r)
            return r.json()

    def voice_search(self, audio_path: str | Path, top_k: int = 1) -> dict[str, Any]:
        return asyncio.run(self.voice_search_async(audio_path, top_k))

    def voice_search_bytes(self, audio_bytes: bytes, filename: str = "audio.wav", top_k: int = 1) -> dict[str, Any]:
        return asyncio.run(self.voice_search_bytes_async(audio_bytes, filename, top_k))

    def list_users(self) -> dict[str, Any]:
        return asyncio.run(self.list_users_async())

    def get_user_face(self, user_id: str) -> dict[str, Any]:
        return asyncio.run(self.get_user_face_async(user_id))

    def get_user_info(self, user_id: str) -> dict[str, Any]:
        return asyncio.run(self.get_user_info_async(user_id))

    def delete_user(self, user_id: str) -> dict[str, Any]:
        return asyncio.run(self.delete_user_async(user_id))

    def sync(self) -> dict[str, Any]:
        return asyncio.run(self.sync_async())

    # ── async API ──

    async def health_async(self) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{self.base_url}/api/health")
            self._check(r)
            return r.json()

    async def register_async(
        self,
        user_id: str,
        name: str,
        face_path: str | Path,
        audio_path: str | Path,
        role: str = "",
        description: str = "",
    ) -> dict[str, Any]:
        face_path = Path(face_path)
        audio_path = Path(audio_path)
        async with httpx.AsyncClient(timeout=60) as client:
            with open(face_path, "rb") as ff, open(audio_path, "rb") as af:
                r = await client.post(
                    f"{self.base_url}/api/user/register",
                    data={"user_id": user_id, "name": name, "role": role, "description": description},
                    files={
                        "face_image": (face_path.name, ff, _guess_mime(face_path)),
                        "voice_audio": (audio_path.name, af, _guess_mime(audio_path)),
                    },
                )
            self._check(r)
            return r.json()

    async def face_search_async(self, image_path: str | Path, top_k: int = 5) -> dict[str, Any]:
        image_path = Path(image_path)
        async with httpx.AsyncClient(timeout=30) as client:
            with open(image_path, "rb") as f:
                r = await client.post(
                    f"{self.base_url}/api/face/search",
                    data={"top_k": str(top_k)},
                    files={"image": (image_path.name, f, _guess_mime(image_path))},
                )
            self._check(r)
            return r.json()

    async def face_verify_async(self, user_id: str, image_path: str | Path) -> dict[str, Any]:
        image_path = Path(image_path)
        async with httpx.AsyncClient(timeout=30) as client:
            with open(image_path, "rb") as f:
                r = await client.post(
                    f"{self.base_url}/api/face/verify",
                    data={"user_id": user_id},
                    files={"image": (image_path.name, f, _guess_mime(image_path))},
                )
            self._check(r)
            return r.json()

    async def face_detect_async(self, image_path: str | Path, max_face_num: int = 10) -> dict[str, Any]:
        image_path = Path(image_path)
        async with httpx.AsyncClient(timeout=30) as client:
            with open(image_path, "rb") as f:
                r = await client.post(
                    f"{self.base_url}/api/face/detect",
                    data={"max_face_num": str(max_face_num)},
                    files={"image": (image_path.name, f, _guess_mime(image_path))},
                )
            self._check(r)
            return r.json()

    async def face_detect_bytes_async(self, image_bytes: bytes, filename: str = "frame.jpg", max_face_num: int = 10) -> dict[str, Any]:
        mime = _guess_mime(Path(filename))
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.base_url}/api/face/detect",
                data={"max_face_num": str(max_face_num)},
                files={"image": (filename, image_bytes, mime)},
            )
            self._check(r)
            return r.json()

    async def voice_search_async(self, audio_path: str | Path, top_k: int = 1) -> dict[str, Any]:
        audio_path = Path(audio_path)
        async with httpx.AsyncClient(timeout=30) as client:
            with open(audio_path, "rb") as f:
                r = await client.post(
                    f"{self.base_url}/api/voice/search",
                    data={"top_k": str(top_k)},
                    files={"audio": (audio_path.name, f, _guess_mime(audio_path))},
                )
            self._check(r)
            return r.json()

    async def voice_search_bytes_async(self, audio_bytes: bytes, filename: str = "audio.wav", top_k: int = 1) -> dict[str, Any]:
        mime = _guess_mime(Path(filename))
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{self.base_url}/api/voice/search",
                data={"top_k": str(top_k)},
                files={"audio": (filename, audio_bytes, mime)},
            )
            self._check(r)
            return r.json()

    async def list_users_async(self) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{self.base_url}/api/user/list")
            self._check(r)
            return r.json()

    async def get_user_face_async(self, user_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{self.base_url}/api/user/{user_id}/face")
            self._check(r)
            return r.json()

    async def get_user_info_async(self, user_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{self.base_url}/api/user/{user_id}")
            self._check(r)
            return r.json()

    async def delete_user_async(self, user_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient() as client:
            r = await client.delete(f"{self.base_url}/api/user/{user_id}")
            self._check(r)
            return r.json()

    async def sync_async(self) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{self.base_url}/api/admin/sync")
            self._check(r)
            return r.json()

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if r.is_error:
            print(f"  HTTP {r.status_code}: {r.text}")
            r.raise_for_status()


_MIME_MAP = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".bmp": "image/bmp",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".pcm": "audio/wav",
}


def _guess_mime(path: Path) -> str:
    return _MIME_MAP.get(path.suffix.lower(), "application/octet-stream")