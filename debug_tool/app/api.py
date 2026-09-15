from __future__ import annotations

from typing import Any

import requests


class ApiClient:
    def __init__(self, api_base: str, timeout: int = 30) -> None:
        self.api_base = api_base.rstrip("/")
        self.timeout = timeout

    def set_api_base(self, api_base: str) -> None:
        self.api_base = api_base.rstrip("/")

    def _url(self, path: str) -> str:
        return f"{self.api_base}{path}"

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = requests.request(method, self._url(path), timeout=self.timeout, **kwargs)
        body = response.text.strip()
        try:
            data = response.json() if body else {}
        except ValueError:
            data = {"raw": body}
        if not response.ok:
            detail = data.get("detail") or data.get("message") or response.reason
            raise RuntimeError(str(detail))
        return data

    def health(self) -> Any:
        return self.request("GET", "/api/health")

    def list_users(self) -> Any:
        return self.request("GET", "/api/user/list")

    def get_user_face(self, user_id: str) -> Any:
        safe_id = requests.utils.quote(user_id, safe="")
        return self.request("GET", f"/api/user/{safe_id}/face")

    def delete_user(self, user_id: str) -> Any:
        safe_id = requests.utils.quote(user_id, safe="")
        return self.request("DELETE", f"/api/user/{safe_id}")

    def clear_users(self) -> Any:
        users = self.list_users().get("users", [])
        deleted: list[str] = []
        failed: list[str] = []
        for user in users:
            user_id = str(user.get("user_id", "")).strip()
            if not user_id:
                continue
            try:
                result = self.delete_user(user_id)
                if result.get("deleted", True):
                    deleted.append(user_id)
                else:
                    failed.append(f"{user_id}: {result.get('message', '未删除')}")
            except Exception as exc:
                failed.append(f"{user_id}: {exc}")
        try:
            sync_result = self.sync_data()
        except Exception as exc:
            sync_result = {"error": str(exc)}
        return {
            "total": len(users),
            "deleted": deleted,
            "failed": failed,
            "sync": sync_result,
        }

    def register_user(self, data: dict[str, str], photo: bytes, audio: bytes) -> Any:
        files = {
            "face_image": ("face.jpg", photo, "image/jpeg"),
            "voice_audio": ("voice.wav", audio, "audio/wav"),
        }
        return self.request("POST", "/api/user/register", data=data, files=files)

    def face_search(self, top_k: int, photo: bytes) -> Any:
        return self.request(
            "POST",
            "/api/face/search",
            data={"top_k": str(top_k)},
            files={"image": ("face-search.jpg", photo, "image/jpeg")},
        )

    def voice_search(self, top_k: int, audio: bytes) -> Any:
        return self.request(
            "POST",
            "/api/voice/search",
            data={"top_k": str(top_k)},
            files={"audio": ("voice-search.wav", audio, "audio/wav")},
        )

    def face_verify(self, user_id: str, photo: bytes) -> Any:
        return self.request(
            "POST",
            "/api/face/verify",
            data={"user_id": user_id},
            files={"image": ("face-verify.jpg", photo, "image/jpeg")},
        )

    def face_detect(self, photo: bytes, max_face_num: int = 10, match_threshold: float = 0.5) -> Any:
        return self.request(
            "POST",
            "/api/face/detect",
            data={
                "max_face_num": str(max_face_num),
                "match_threshold": str(match_threshold),
            },
            files={"image": ("face-detect.jpg", photo, "image/jpeg")},
        )

    def sync_data(self) -> Any:
        return self.request("POST", "/api/admin/sync")


class RobotCommandClient:
    def __init__(self, shell_proxy_url: str, timeout: int = 600) -> None:
        self.shell_proxy_url = self._normalize_url(shell_proxy_url)
        self.timeout = timeout

    def set_shell_proxy_url(self, shell_proxy_url: str) -> None:
        self.shell_proxy_url = self._normalize_url(shell_proxy_url)

    def _normalize_url(self, url: str) -> str:
        clean = url.strip().rstrip("/")
        return clean if clean.endswith("/run") else f"{clean}/run"

    def _is_success(self, value: Any) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return value != 0
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "success", "succeeded", "ok", "done", "completed", "complete", "1", "成功"}:
                return True
            if normalized in {"false", "fail", "failed", "failure", "error", "timeout", "0", "失败"}:
                return False
        return bool(value)

    def run(self, cmd: str, timeout: int | float | None = None) -> Any:
        response = requests.post(
            self.shell_proxy_url,
            json={"cmd": cmd},
            timeout=self.timeout if timeout is None else timeout,
        )
        body = response.text.strip()
        try:
            data = response.json() if body else {}
        except ValueError:
            data = {"output": body, "success": False}
        if not response.ok:
            detail = data.get("output") or data.get("detail") or response.reason
            raise RuntimeError(str(detail))
        if not self._is_success(data.get("success", False)):
            raise RuntimeError(str(data.get("output", "命令执行失败")))
        return data
