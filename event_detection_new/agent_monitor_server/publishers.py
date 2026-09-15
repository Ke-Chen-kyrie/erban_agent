"""Best-effort publishing to the existing Agent and dashboard endpoints."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

import aiohttp

from .config import MonitorConfig
from .schemas import DetectedEvent


PostJson = Callable[[str, dict[str, Any]], Awaitable[bool]]


@dataclass(frozen=True)
class PublishOutcome:
    agent_succeeded: bool
    dashboard_succeeded: bool


class EventPublishers:
    def __init__(self, config: MonitorConfig, *, post_json: PostJson | None = None) -> None:
        self._config = config
        self._injected_post = post_json
        self._session: aiohttp.ClientSession | None = None

    async def publish_match(
        self,
        event: DetectedEvent,
        image_bytes: bytes,
        task_id: str,
        sequence: int,
        detected_at: str,
    ) -> PublishOutcome:
        image_base64 = base64.b64encode(image_bytes).decode("ascii")
        agent_description = event.description
        if event.name:
            agent_description = f"{event.description}（人员：{event.name}）"
        agent_payload = {
            "event_type": "action",
            "actions": [
                {
                    "event_name": event.event_name,
                    "description": agent_description,
                    "name": event.name,
                    "image_base64": image_base64,
                    "image_mime_type": "image/jpeg",
                    "video_base64": "",
                    "video_mime_type": "video/avi",
                    "task_id": task_id,
                    "sequence": sequence,
                    "detected_at": detected_at,
                }
            ],
        }
        dashboard_payload = {
            "event": event.event_name,
            "description": event.description,
            "level": "alert",
            "name": event.name,
            "images": [image_base64],
            "videos": [],
            "task_id": task_id,
            "sequence": sequence,
            "detected_at": detected_at,
        }
        agent_ok, dashboard_ok = await asyncio.gather(
            self._safe_post(self._config.agent_webhook_url, agent_payload),
            self._safe_post(self._config.system_event_url, dashboard_payload),
        )
        return PublishOutcome(agent_ok, dashboard_ok)

    async def _safe_post(self, url: str, payload: dict[str, Any]) -> bool:
        if not url:
            return False
        try:
            post = self._injected_post or self._post_json
            return bool(await post(url, payload))
        except Exception:
            return False

    async def _post_json(self, url: str, payload: dict[str, Any]) -> bool:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self._config.push_timeout_seconds)
            self._session = aiohttp.ClientSession(timeout=timeout)
        async with self._session.post(url, json=payload) as response:
            await response.read()
            return response.status < 400

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
