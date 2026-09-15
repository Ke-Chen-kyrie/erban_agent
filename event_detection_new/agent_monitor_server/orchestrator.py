"""Continuous-frame VLM orchestration using an Agent-owned prompt."""

from __future__ import annotations

import base64
import io
from typing import Any

import cv2
from openai import AsyncOpenAI
from PIL import Image

from .config import MonitorConfig
from .schemas import MonitorRequest
from .video_source import CapturedFrame


class ContinuousFrameOrchestrator:
    def __init__(
        self,
        config: MonitorConfig,
        request: MonitorRequest,
        *,
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._request = request
        self._client = client or AsyncOpenAI(
            base_url=config.main_api_base,
            api_key=config.api_key,
            timeout=300,
        )
        self._history: list[dict[str, Any]] = []
        self._pending_user: dict[str, Any] | None = None

    def reset(self, request: MonitorRequest) -> None:
        self._request = request
        self._history.clear()
        self._pending_user = None

    async def infer(self, frame: CapturedFrame) -> str:
        user_message = {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": f"frame_captured_at={frame.captured_at}",
                },
                {
                    "type": "image_url",
                    "image_url": {"url": self._frame_data_url(frame)},
                },
            ],
        }
        messages = [
            {"role": "system", "content": self._request.prompt},
            *self._history,
            user_message,
        ]
        request_options: dict[str, Any] = {}
        if self._config.disable_thinking:
            request_options["extra_body"] = {
                "enable_thinking": False,
                "chat_template_kwargs": {
                    "enable_thinking": False,
                    "enable_thinking_assert": False,
                },
            }
        response = await self._client.chat.completions.create(
            model=self._config.main_model,
            messages=messages,
            max_tokens=self._config.max_tokens,
            temperature=self._config.temperature,
            **request_options,
        )
        self._pending_user = user_message
        return (response.choices[0].message.content or "").strip()

    def record_response(self, text: str) -> None:
        if self._pending_user is None:
            return
        self._history.extend(
            [self._pending_user, {"role": "assistant", "content": text}]
        )
        self._pending_user = None
        keep_messages = max(0, self._config.max_context_turns) * 2
        if keep_messages == 0:
            self._history.clear()
        elif len(self._history) > keep_messages:
            self._history = self._history[-keep_messages:]

    @staticmethod
    def _frame_data_url(frame: CapturedFrame) -> str:
        image = frame.image
        if frame.bgr_input:
            image = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        output = io.BytesIO()
        Image.fromarray(image).save(output, format="JPEG", quality=85)
        encoded = base64.b64encode(output.getvalue()).decode("ascii")
        return f"data:image/jpeg;base64,{encoded}"
