# -*- coding: utf-8 -*-
import base64
import os
import re
import time

from concurrent.futures import ThreadPoolExecutor, as_completed
from openai import OpenAI
from PIL import Image
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from prompt import (
    DETAILED_SUMMARY_PROMPT,
    BATCH_COMPRESS_PROMPT,
    EMPTY_CHUNK_SUMMARY_TEMPLATE,
)

# ============================================================
# Helper: encode image to base64 data URL
# ============================================================

def _encode_image_base64(image_path: str, max_pixels: int = 0) -> str:
    """Read an image file and return a base64 data URL for the OpenAI API.

    If *max_pixels* > 0 and the image exceeds that budget, it is
    down-scaled (preserving aspect ratio) so that width*height <= max_pixels.
    """
    if image_path.startswith("data:"):
        if max_pixels > 0:
            match = re.match(r"data:image/\w+;base64,(.+)", image_path)
            if match:
                img = Image.open(io.BytesIO(base64.b64decode(match.group(1))))
                w, h = img.size
                if w * h > max_pixels:
                    scale = (max_pixels / (w * h)) ** 0.5
                    new_w = max(1, int(w * scale))
                    new_h = max(1, int(h * scale))
                    img = img.resize((new_w, new_h), Image.LANCZOS)
                    buf = io.BytesIO()
                    img.save(buf, format="JPEG")
                    data = base64.b64encode(buf.getvalue()).decode("utf-8")
                    return f"data:image/jpeg;base64,{data}"
        return image_path
    ext = os.path.splitext(image_path)[1].lower()
    mime_map = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
    }
    mime_type = mime_map.get(ext, "image/jpeg")

    if max_pixels > 0:
        img = Image.open(image_path)
        w, h = img.size
        if w * h > max_pixels:
            scale = (max_pixels / (w * h)) ** 0.5
            new_w = max(1, int(w * scale))
            new_h = max(1, int(h * scale))
            img = img.resize((new_w, new_h), Image.LANCZOS)
            buf = io.BytesIO()
            save_fmt = "PNG" if ext == ".png" else "JPEG"
            img.save(buf, format=save_fmt)
            data = base64.b64encode(buf.getvalue()).decode("utf-8")
            return f"data:{mime_type};base64,{data}"

    with open(image_path, "rb") as f:
        data = base64.b64encode(f.read()).decode("utf-8")

    return f"data:{mime_type};base64,{data}"


# JoyAI-VL interaction server requires at least one image_url frame in every
# message. The long-term compression step is text-only, so we attach a small
# neutral placeholder frame just to satisfy the server; the compression itself
# is driven entirely by the text prompt.
_PLACEHOLDER_DATA_URL: str = ""


def _placeholder_image_data_url() -> str:
    global _PLACEHOLDER_DATA_URL
    if not _PLACEHOLDER_DATA_URL:
        img = Image.new("RGB", (64, 64), (128, 128, 128))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=60)
        data = base64.b64encode(buf.getvalue()).decode("utf-8")
        _PLACEHOLDER_DATA_URL = f"data:image/jpeg;base64,{data}"
    return _PLACEHOLDER_DATA_URL


def _has_image_content(messages: list) -> bool:
    """True if any message already carries an image frame to the VLM."""
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for item in content:
            if isinstance(item, dict) and item.get("type") in ("image_url", "image"):
                return True
    return False


def _ensure_interaction_frame(messages: list) -> list:
    """Attach a placeholder image to the last user message if none is present.

    JoyAI-VL's interaction endpoint rejects text-only payloads with
    'interaction server requires at least one image_url frame'. Only the
    long-term text compression hits this path; frame-bearing summaries are
    left untouched.
    """
    if _has_image_content(messages):
        return messages
    messages = list(messages)
    for idx in range(len(messages) - 1, -1, -1):
        if messages[idx].get("role") != "user":
            continue
        content = messages[idx].get("content")
        frame = {"type": "image_url", "image_url": {"url": _placeholder_image_data_url()}}
        if isinstance(content, list):
            messages[idx] = {**messages[idx], "content": list(content) + [frame]}
        else:
            messages[idx] = {
                **messages[idx],
                "content": [
                    {"type": "text", "text": content or ""},
                    frame,
                ],
            }
        break
    return messages


# ============================================================
# SummarizerModel (OpenAI API client)
# ============================================================

class SummarizerModel:
    def __init__(
        self,
        model_name: str = "/tmp/models/Qwen3-VL-4B-Instruct",
        api_base: str = "http://localhost:8065/v1",
        api_key: str = "EMPTY",
        longterm_model_name: str = "",
        longterm_api_base: str = "",
        mid_term_max_tokens: int = 5000,
        mid_term_target_tokens: int = 0,
        long_term_max_tokens: int = 1200,
        long_term_target_tokens: int = 0,
        key_frames_per_chunk: int = 8,
        max_pixels: int = 0,
        prompt_phase_seconds: float = 10.0,
        mid_term_temperature: float = 0.1,
        mid_term_top_p: float = 0.9,
        mid_term_top_k: int = -1,
        mid_term_repetition_penalty: float = 1.0,
        mid_term_presence_penalty: float = 0.0,
        long_term_temperature: float = 0.1,
        long_term_top_p: float = 0.9,
        long_term_top_k: int = -1,
        long_term_repetition_penalty: float = 1.0,
        long_term_presence_penalty: float = 0.0,
        disable_thinking: bool = True,
        debug: bool = False,
    ):
        self.model_name = model_name
        self.longterm_model_name = longterm_model_name or model_name
        self.mid_term_max_tokens = mid_term_max_tokens
        self.mid_term_target_tokens = mid_term_target_tokens
        self.long_term_max_tokens = long_term_max_tokens
        self.long_term_target_tokens = long_term_target_tokens
        self.key_frames_per_chunk = key_frames_per_chunk
        self.max_pixels = max_pixels
        self.prompt_phase_seconds = prompt_phase_seconds
        self.mid_term_temperature = mid_term_temperature
        self.mid_term_top_p = mid_term_top_p
        self.mid_term_top_k = mid_term_top_k
        self.mid_term_repetition_penalty = mid_term_repetition_penalty
        self.mid_term_presence_penalty = mid_term_presence_penalty
        self.long_term_temperature = long_term_temperature
        self.long_term_top_p = long_term_top_p
        self.long_term_top_k = long_term_top_k
        self.long_term_repetition_penalty = long_term_repetition_penalty
        self.long_term_presence_penalty = long_term_presence_penalty
        self.disable_thinking = disable_thinking
        self.debug = debug

        # 中期摘要客户端（多模态，带图片）
        # 显式设置总超时并关闭重试：摘要端点不可达时必须快速失败，
        # 绝不能默认 600s×多次重试而长时间阻塞主程序。
        self._client = OpenAI(
            api_key=api_key,
            base_url=api_base,
            timeout=60.0,
            max_retries=0,
        )

        # 长期压缩客户端（纯文本）—— 如果未指定则复用中期客户端
        if longterm_api_base and longterm_api_base != api_base:
            self._longterm_client = OpenAI(
                api_key=api_key,
                base_url=longterm_api_base,
                timeout=60.0,
                max_retries=0,
            )
        else:
            self._longterm_client = self._client

    def preencode_frame(self, image_path: str) -> tuple[dict, float]:
        """Pre-encode one frame for mid-term summary use during streaming."""
        start_time = time.time()
        data_url = _encode_image_base64(image_path, max_pixels=self.max_pixels)
        return {
            "path": image_path,
            "data_url": data_url,
        }, time.time() - start_time

    def _chat(self, messages: list, max_tokens: int, temperature: float = 0.3, top_p: float = 0.9,
              top_k: int = -1, repetition_penalty: float = 1.0,
              presence_penalty: float = 0.0,
              client: OpenAI = None, model_name: str = None) -> tuple[str, dict]:
        """Call the vLLM OpenAI API server. Returns (text, usage_dict)."""
        client = client or self._client
        model_name = model_name or self.model_name
        messages = _ensure_interaction_frame(messages)
        extra_body = {"greedy": False}
        if self.disable_thinking:
            # Support both DashScope and vLLM/SGLang Qwen chat templates.
            extra_body["enable_thinking"] = False
            extra_body["chat_template_kwargs"] = {
                "enable_thinking": False,
                "enable_thinking_assert": False,
            }
        if top_k > 0:
            extra_body["top_k"] = top_k
        if repetition_penalty != 1.0:
            extra_body["repetition_penalty"] = repetition_penalty
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            top_p=top_p,
            presence_penalty=presence_penalty,
            extra_body=extra_body,
        )
        text = response.choices[0].message.content.strip() if response.choices else ""
        usage = {}
        if response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        return text, usage

    def _parse_time_value(self, text: str):
        if text is None:
            return None
        value = text.strip()
        if value.endswith("s"):
            value = value[:-1]
        try:
            return float(value)
        except ValueError:
            return None

    @staticmethod
    def _split_frame_range(frame_range: str) -> tuple:
        """Split a frame_range string into (start, end).

        Handles both "0s-10s" and "0.0 seconds ~ 89.0 seconds" formats.
        """
        if " ~ " in frame_range:
            parts = frame_range.split(" ~ ", 1)
            return parts[0].strip(), parts[-1].strip()
        if "-" in frame_range:
            parts = frame_range.split("-", 1)
            return parts[0].strip(), parts[-1].strip()
        return frame_range.strip(), frame_range.strip()

    def _parse_time_range_bounds(self, time_range: str) -> tuple:
        if not time_range or "-" not in time_range:
            return None, None
        start_text, end_text = time_range.split("-", 1)
        return self._parse_time_value(start_text), self._parse_time_value(end_text)

    def _format_time_value(self, value: float) -> str:
        if value is None:
            return "0s"
        if abs(value - round(value)) < 1e-6:
            return f"{int(round(value))}s"
        return f"{value:.3f}".rstrip("0").rstrip(".") + "s"

    def _build_range_from_frame_indices(self, frame_time_ranges: list, start_idx: int, end_idx: int) -> str:
        start_value = float(start_idx)
        end_value = float(end_idx + 1)

        if start_idx < len(frame_time_ranges):
            parsed_start, _ = self._parse_time_range_bounds(frame_time_ranges[start_idx])
            if parsed_start is not None:
                start_value = parsed_start
        if end_idx < len(frame_time_ranges):
            _, parsed_end = self._parse_time_range_bounds(frame_time_ranges[end_idx])
            if parsed_end is not None:
                end_value = parsed_end

        if end_value <= start_value:
            end_value = start_value + 1.0

        return f"{self._format_time_value(start_value)}-{self._format_time_value(end_value)}"

    def _sample_sorted_indices(self, indices: list, budget: int) -> list:
        if budget <= 0 or not indices:
            return []
        if len(indices) <= budget:
            return list(indices)
        if budget == 1:
            return [indices[len(indices) // 2]]

        selected_positions = []
        last_pos = -1
        total = len(indices)
        for slot in range(budget):
            target = round(slot * (total - 1) / (budget - 1))
            target = max(target, last_pos + 1)
            max_allowed = total - (budget - slot)
            target = min(target, max_allowed)
            selected_positions.append(target)
            last_pos = target
        return [indices[pos] for pos in selected_positions]

    def _get_frame_prompt_time_range(self, frame: dict) -> str:
        return frame.get("source_time_range") or frame.get("time_range") or "unknown"

    def _build_prompt_phases(self, key_frames: list) -> list:
        return [
            {
                "time_range": self._get_frame_prompt_time_range(frame),
                "frames": [frame],
            }
            for frame in key_frames
        ]

    def _build_mid_term_debug_input(
        self,
        chunk_index: int,
        frame_range: str,
        frame_count: int,
        active_query: str,
        prompt_text: str,
        key_frames: list,
        phases: list,
        temperature: float,
    ) -> dict:
        content = [{"type": "text", "text": prompt_text}]
        for phase in phases:
            content.append({"type": "text", "text": f"<{phase['time_range']}>"})
            for frame in phase["frames"]:
                content.append({
                    "type": "image_path",
                    "image_path": frame["path"],
                    "max_pixels": self.max_pixels,
                })
        return {
            "stage": "mid_term",
            "model_name": self.model_name,
            "max_tokens": self.mid_term_max_tokens,
            "target_token_count": self.mid_term_target_tokens,
            "temperature": temperature,
            "top_p": self.mid_term_top_p,
            "chunk_index": chunk_index,
            "frame_range": frame_range,
            "frame_count": frame_count,
            "active_query": active_query or "(none)",
            "phase_seconds": self.prompt_phase_seconds,
            "phases": [
                {
                    "time_range": phase.get("time_range"),
                    "frame_count": len(phase.get("frames", [])),
                    "frame_indices": [frame.get("frame_index") for frame in phase.get("frames", [])],
                    "frame_time_ranges": [frame.get("time_range") for frame in phase.get("frames", [])],
                }
                for phase in phases
            ],
            "key_frames": [
                {
                    "time_range": frame.get("time_range"),
                    "source_time_range": frame.get("source_time_range"),
                    "path": frame.get("path"),
                    "frame_index": frame.get("frame_index"),
                    "range_start_index": frame.get("range_start_index"),
                    "range_end_index": frame.get("range_end_index"),
                }
                for frame in key_frames
            ],
            "messages": [{"role": "user", "content": content}],
            "prompt_text": prompt_text,
        }

    def _build_long_term_debug_input(
        self,
        merged_range: str,
        active_query: str,
        prompt_text: str,
        mid_term_summaries: list,
        temperature: float,
    ) -> dict:
        return {
            "stage": "long_term",
            "model_name": self.longterm_model_name,
            "max_tokens": self.long_term_max_tokens,
            "target_token_count": self.long_term_target_tokens,
            "temperature": temperature,
            "top_p": self.long_term_top_p,
            "merged_range": merged_range,
            "active_query": active_query or "(none)",
            "source_summaries": [
                {
                    "chunk_index": entry.get("chunk_index"),
                    "frame_range": entry.get("frame_range"),
                    "summary_text": entry.get("summary_text"),
                }
                for entry in mid_term_summaries
            ],
            "messages": [{"role": "user", "content": prompt_text}],
            "prompt_text": prompt_text,
        }

    def select_key_frames(
        self,
        image_paths: list,
        frame_time_ranges: list,
        key_frame_indices: list,
        cached_frame_payloads: list = None,
    ) -> list:
        """Select up to key_frames_per_chunk frames and carry cached payloads when available."""
        del key_frame_indices  # Mid-term summaries are now driven only by timestamped frames.

        n = len(image_paths)
        budget = self.key_frames_per_chunk
        if n == 0:
            return []

        if budget <= 0 or n <= budget:
            selected_indices = list(range(n))
        else:
            selected_indices = self._sample_sorted_indices(list(range(n)), budget)

        key_frames = []
        for pos, idx in enumerate(selected_indices):
            left_idx = 0 if pos == 0 else (selected_indices[pos - 1] + idx) // 2 + 1
            right_idx = n - 1 if pos == len(selected_indices) - 1 else (idx + selected_indices[pos + 1]) // 2
            left_idx = min(max(left_idx, 0), n - 1)
            right_idx = min(max(right_idx, left_idx), n - 1)

            cached_frame = None
            if cached_frame_payloads and idx < len(cached_frame_payloads):
                cached_frame = cached_frame_payloads[idx]

            path = image_paths[idx]
            if cached_frame and cached_frame.get("path"):
                path = cached_frame["path"]
            if not path.startswith("data:") and not os.path.exists(path):
                continue

            source_time_range = frame_time_ranges[idx] if idx < len(frame_time_ranges) else None
            key_frame = {
                "path": path,
                "source_time_range": source_time_range,
                "time_range": self._build_range_from_frame_indices(frame_time_ranges, left_idx, right_idx),
                "frame_index": idx,
                "range_start_index": left_idx,
                "range_end_index": right_idx,
            }
            if cached_frame and cached_frame.get("captured_at"):
                key_frame["captured_at"] = cached_frame["captured_at"]
            if cached_frame and cached_frame.get("data_url"):
                key_frame["data_url"] = cached_frame["data_url"]
            key_frames.append(key_frame)
        return key_frames

    def _build_mid_term_length_instruction(self) -> str:
        if self.mid_term_target_tokens > 0:
            return f"，目标约 {self.mid_term_target_tokens} tokens"
        return ""

    def _build_long_term_length_instruction(self) -> str:
        if self.long_term_target_tokens > 0:
            return f"，目标约 {self.long_term_target_tokens} tokens"
        return ""

    def generate_detailed_summary(
        self,
        chunk_index: int,
        frame_range: str,
        key_frames: list,
        frame_count: int,
        active_query: str = "",
    ) -> tuple:
        """Generate a detailed multimodal summary for a chunk (Tier 2).

        Returns (summary_text, debug_input_or_None).
        """
        if not key_frames:
            return EMPTY_CHUNK_SUMMARY_TEMPLATE.format(frame_range=frame_range), None

        length_instruction = self._build_mid_term_length_instruction()
        mid_term_temperature = self.mid_term_temperature
        prompt_text = DETAILED_SUMMARY_PROMPT.format(
            frame_range=frame_range,
            chunk_index=chunk_index,
            length_instruction=length_instruction,
            preferred_time_span=f"{self.prompt_phase_seconds:g} seconds",
        )

        phases = self._build_prompt_phases(key_frames)

        debug_input = None
        if self.debug:
            debug_input = self._build_mid_term_debug_input(
                chunk_index=chunk_index,
                frame_range=frame_range,
                frame_count=frame_count,
                active_query=active_query,
                prompt_text=prompt_text,
                key_frames=key_frames,
                phases=phases,
                temperature=mid_term_temperature,
            )

        frames_to_encode = [
            frame for phase in phases for frame in phase["frames"]
            if not frame.get("data_url")
        ]
        if frames_to_encode:
            max_px = self.max_pixels
            with ThreadPoolExecutor(max_workers=min(len(frames_to_encode), 4)) as pool:
                futures = {
                    pool.submit(_encode_image_base64, f["path"], max_px): f
                    for f in frames_to_encode
                }
                for fut in as_completed(futures):
                    frame = futures[fut]
                    try:
                        frame["data_url"] = fut.result()
                    except Exception as e:
                        print(f"WARNING: Failed to encode image {frame['path']}: {e}")

        content_list = [{"type": "text", "text": prompt_text}]
        for phase in phases:
            content_list.append({"type": "text", "text": f"<{phase['time_range']}>"})
            for frame in phase["frames"]:
                if frame.get("captured_at"):
                    content_list.append({
                        "type": "text",
                        "text": "本地采集时间（辅助记录，不用于排序）：" + frame["captured_at"],
                    })
                data_url = frame.get("data_url")
                if data_url:
                    content_list.append({
                        "type": "image_url",
                        "image_url": {"url": data_url},
                    })

        messages = [{"role": "user", "content": content_list}]
        summary_text, usage = self._chat(
            messages,
            max_tokens=self.mid_term_max_tokens,
            temperature=mid_term_temperature,
            top_p=self.mid_term_top_p,
            top_k=self.mid_term_top_k,
            repetition_penalty=self.mid_term_repetition_penalty,
            presence_penalty=self.mid_term_presence_penalty,
        )
        return summary_text, usage, debug_input

    def batch_compress_to_longterm(
        self,
        existing_longterm: str,
        mid_term_summaries: list,
    ) -> tuple:
        """
        Compress mid-term summaries and append to existing long-term memory.

        Only the new mid-term summaries are compressed (existing long-term is
        NOT re-compressed), then appended to the existing block.

        mid_term_summaries: list of dicts with keys 'frame_range', 'summary_text', etc.
        Returns (merged_text, token_usage, compressed_new_text, debug_input_or_None).
        """

        if not mid_term_summaries:
            return existing_longterm, {}, "", None

        # 计算合并后的时间范围：取第一个 chunk 的起始和最后一个 chunk 的结束
        # frame_range 可能是 "0s-10s" 或 "0.0 seconds ~ 89.0 seconds" 两种格式
        first_range = mid_term_summaries[0]["frame_range"]
        last_range = mid_term_summaries[-1]["frame_range"]
        merged_start = self._split_frame_range(first_range)[0]
        merged_end = self._split_frame_range(last_range)[1]
        merged_range = f"{merged_start}-{merged_end}"

        # Step 1: Compress the new mid-term summaries into a single block
        summary_parts = []
        for entry in mid_term_summaries:
            summary_parts.append(
                f"<{entry['frame_range']}>\n{entry['summary_text']}"
            )
        summaries_text = "\n\n".join(summary_parts)

        active_query = ""
        for entry in mid_term_summaries:
            candidate_query = entry.get("query")
            if candidate_query:
                active_query = candidate_query
                break

        long_term_temperature = self.long_term_temperature
        length_instruction = self._build_long_term_length_instruction()
        prompt_text = BATCH_COMPRESS_PROMPT.format(
            summaries_text=summaries_text,
            length_instruction=length_instruction,
            merged_range=merged_range,
        )
        debug_input = None
        if self.debug:
            debug_input = self._build_long_term_debug_input(
                merged_range=merged_range,
                active_query=active_query,
                prompt_text=prompt_text,
                mid_term_summaries=mid_term_summaries,
                temperature=long_term_temperature,
            )

        compressed_new, usage = self._chat(
            [{"role": "user", "content": prompt_text}],
            max_tokens=self.long_term_max_tokens,
            temperature=long_term_temperature,
            top_p=self.long_term_top_p,
            top_k=self.long_term_top_k,
            repetition_penalty=self.long_term_repetition_penalty,
            presence_penalty=self.long_term_presence_penalty,
            client=self._longterm_client,
            model_name=self.longterm_model_name,
        )
        # 在压缩结果前加上合并时间范围标记
        compressed_new = f"<{merged_range}>\n{compressed_new}"

        # Step 2: Append to existing long-term memory
        if existing_longterm:
            merged = existing_longterm.rstrip() + "\n\n" + compressed_new
        else:
            merged = compressed_new

        return merged, usage, compressed_new, debug_input

    def shutdown(self):
        """No-op: the vLLM server is managed externally."""
        pass
