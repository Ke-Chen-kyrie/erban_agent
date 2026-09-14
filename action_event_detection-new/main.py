#!/usr/bin/env python3
"""
Action Event Detection — 单文件一体化脚本。

摄像头采集 → 远程 vLLM 推理 → 检测到动作时 POST /action_event。

Usage:
  python main.py
  python main.py --camera 1
  python main.py --source foxglove
"""

import argparse
import asyncio
import base64
import io
import os
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import cv2
import numpy as np
from openai import AsyncOpenAI
from PIL import Image

from prompt import GESTURE_DETECT_PROMPT
from face_detect import _get_ident_client, draw_face_detect, detect_faces

# ── config defaults ────────────────────────────────────────────
DEFAULT_SYSTEM_PROMPT = GESTURE_DETECT_PROMPT


class DetectionState:
    """Shared state for detection enable/disable control."""
    def __init__(self):
        self.enabled = True


def create_control_app(state: DetectionState) -> web.Application:
    app = web.Application()

    async def handle_detection(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            raise web.HTTPBadRequest(text="invalid JSON")

        enable = body.get("enable")
        if not isinstance(enable, bool):
            raise web.HTTPBadRequest(text='missing or invalid "enable" (must be bool)')

        prev = state.enabled
        state.enabled = enable
        print(f"\n[{ts()}] Detection {'enabled' if enable else 'disabled'}", flush=True)
        return web.json_response({"enable": enable, "previous": prev})

    app.router.add_post("/detection", handle_detection)
    return app


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def encode_frame(frame: np.ndarray, bgr_input: bool = True) -> str:
    if bgr_input:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(frame)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def normalize_output(text: str) -> str:
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        return "</silence>"
    marker_positions = []
    for marker in ("</response>", "</silence>"):
        idx = raw.find(marker)
        if idx != -1:
            marker_positions.append((idx, marker))
    if marker_positions:
        _, marker = min(marker_positions, key=lambda item: item[0])
        if marker == "</silence>":
            return "</silence>"
        response_text = raw.split(marker, 1)[1].strip()
        if not response_text:
            return "</response>"
        first_line = " ".join(response_text.splitlines()[0].split())
        return f"</response> {first_line}" if first_line else "</response>"
    first_line = " ".join(raw.splitlines()[0].split())
    return f"</response> {first_line}" if first_line else "</silence>"


def _print_vlm_messages(messages: list[dict], model_name: str, generation_kwargs: dict) -> None:
    """Print messages sent to VLM, truncating base64 image data for readability."""
    sep = "=" * 60
    print(f"\n{sep}\n>>> VLM CALL: model={model_name}")
    for key, val in generation_kwargs.items():
        if key != "extra_body":
            print(f"    {key}={val}")
    print(sep)
    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            print(f"[{i}] role={role}")
            for item in content:
                if isinstance(item, dict):
                    t = item.get("type", "?")
                    if t == "text":
                        text = str(item.get("text", ""))
                        print(f"    text({len(text)} chars): {text[:300]}{'...' if len(text) > 300 else ''}")
                    elif t in ("image", "image_url"):
                        url = str(item.get("image", item.get("image_url", {}).get("url", "")))
                        if url.startswith("data:"):
                            print(f"    {t}: data:... (base64, {len(url)} chars)")
                        else:
                            print(f"    {t}: {url[:200]}")
                    else:
                        print(f"    {t}: {str(item)[:200]}")
                else:
                    print(f"    {str(item)[:200]}")
        elif isinstance(content, str):
            print(f"[{i}] role={role} content({len(content)} chars): {content[:300]}{'...' if len(content) > 300 else ''}")
        else:
            print(f"[{i}] role={role} content={str(content)[:200]}")
    print(f"{sep}\n", flush=True)


def parse_actions(text: str) -> list[dict]:
    """Parse multi-person action response from VLM.

    "</response> [wave] 小明 | [nod] 小红"
    -> [{"action_type": "wave", "subject": "小明"}, {"action_type": "nod", "subject": "小红"}]
    """
    body = text.removeprefix("</response>").strip()
    actions = []
    for segment in body.split("|"):
        segment = segment.strip()
        if not segment:
            continue
        parts = segment.split(None, 1)
        if not parts:
            continue
        label = parts[0].strip("[]")
        subject = parts[1].strip() if len(parts) > 1 else "路人"
        actions.append({"action_type": label, "subject": subject})
    return actions


async def post_action_events(
    events: list[dict],
    host: str,
    port: int,
) -> None:
    """POST 动作事件列表到 /action_event。"""
    import aiohttp

    url = f"http://{host}:{port}/action_event"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=events, timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                status = resp.status
                if status < 400:
                    subjects = ", ".join(e["subject"] for e in events)
                    labels = ", ".join(e["action_type"] for e in events)
                    print(f"[{ts()}] event pushed: [{labels}] subjects=[{subjects}] status={status}")
                else:
                    print(f"[{ts()}] event push failed: status={status}", file=sys.stderr)
    except Exception as exc:
        print(f"[{ts()}] event push error: {exc}", file=sys.stderr)


def _reset_context() -> tuple[list, int]:
    """Clear chunk context and reset counters. Returns fresh (chunk_messages, chunk_turn_count)."""
    print(f"[{ts()}] Context cleared", flush=True)
    return [], 0


async def main_async(args: argparse.Namespace) -> None:
    detection_state = DetectionState()

    # ── start control API server ──
    control_app = create_control_app(detection_state)
    control_runner = web.AppRunner(control_app)
    await control_runner.setup()
    control_site = web.TCPSite(control_runner, "0.0.0.0", args.control_port)
    await control_site.start()
    print(f"[{ts()}] Control API: http://0.0.0.0:{args.control_port}/detection")

    # ── init VLM client ──
    client = AsyncOpenAI(
        base_url=args.main_api_base,
        api_key=args.api_key,
        timeout=args.request_timeout,
    )

    # ── init camera ──
    if args.source == "foxglove":
        from foxglove_client import FoxgloveImageCapture

        cap = FoxgloveImageCapture(
            topic=args.foxglove_topic, url=args.foxglove_url or None
        )
        bgr_input = False
        source_desc = f"Foxglove topic={args.foxglove_topic}"
    else:
        cap = cv2.VideoCapture(args.camera)
        if not cap.isOpened():
            print(f"Error: cannot open camera {args.camera}", file=sys.stderr)
            sys.exit(1)
        if args.width > 0:
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        if args.height > 0:
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        bgr_input = True
        source_desc = f"Camera {args.camera} ({args.width}x{args.height})"

    session_id = uuid.uuid4().hex[:12]
    session_start = time.time()

    # ── fetch known face names from ident service ──
    known_names: list[str] = []
    try:
        ident_client = _get_ident_client()
        if ident_client:
            result = await ident_client.list_users_async()
            users = result.get("users", []) if isinstance(result, dict) else []
            known_names = [u.get("name", "") for u in users if u.get("name")]
            if known_names:
                print(f"[{ts()}] Known users: {', '.join(known_names)}")
    except Exception as e:
        print(f"[{ts()}] Failed to fetch user list: {e}", file=sys.stderr)

    system_prompt = args.system_prompt
    if known_names:
        system_prompt += f"\n\n已知人员名单（仅限以下名字，不允许输出其他名字）: {', '.join(known_names)}"

    print(f"[{ts()}] Session: {session_id}")
    print(f"[{ts()}] Source: {source_desc}")
    print(f"[{ts()}] VLM: {args.main_model} @ {args.main_api_base}")
    print(f"[{ts()}] Action event: http://{args.action_event_host}:{args.action_event_port}/action_event")
    print("-" * 50)

    frame_queue: asyncio.Queue = asyncio.Queue(maxsize=2)

    # ── producer: capture + face detect ──
    async def produce_frames():
        frame_count = 0
        last_capture = 0.0
        was_enabled = detection_state.enabled

        while True:
            if not detection_state.enabled:
                was_enabled = False
                await asyncio.sleep(0.1)
                continue

            if detection_state.enabled and not was_enabled:
                frame_count = 0
                last_capture = 0.0
            was_enabled = True

            if args.source == "camera":
                ret, frame = cap.read()
            else:
                ret, frame = cap.read(timeout=1.0)

            if not ret or frame is None:
                print(f"[{ts()}] Frame read failed, retrying...", file=sys.stderr)
                await asyncio.sleep(0.5)
                continue

            now = time.time()
            if now - last_capture < 0.05:
                await asyncio.sleep(0.01)
                continue

            last_capture = now
            frame_count += 1

            t_face = time.perf_counter()
            if bgr_input:
                faces = detect_faces(frame)
                annotated = draw_face_detect(frame, faces)
            else:
                faces = detect_faces(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))
                annotated = draw_face_detect(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR), faces)
            t_face = time.perf_counter() - t_face
            img_b64 = encode_frame(annotated, bgr_input=True)

            if args.save_annotated:
                save_dir = Path(args.save_dir)
                save_dir.mkdir(parents=True, exist_ok=True)
                filename = f"{session_id}_{frame_count:06d}.jpg"
                cv2.imwrite(str(save_dir / filename), annotated)

            await frame_queue.put({
                "frame_count": frame_count,
                "faces": faces,
                "img_b64": img_b64,
                "t_face": t_face,
            })

    # ── consumer: VLM inference + event push ──
    async def consume_frames():
        chunk_messages: list[dict] = []
        chunk_turn_count = 0
        last_process_time = time.time()
        was_enabled = detection_state.enabled

        generation_kwargs = {
            "max_tokens": args.max_tokens,
            "temperature": args.temperature,
            "top_p": args.top_p,
            "presence_penalty": args.presence_penalty,
            "extra_body": {
                "top_k": args.top_k,
                "repetition_penalty": args.repetition_penalty,
                "skip_special_tokens": False,
                "greedy": False,
            },
        }

        while True:
            if not detection_state.enabled:
                was_enabled = False
                await asyncio.sleep(0.1)
                continue

            if detection_state.enabled and not was_enabled:
                chunk_messages, chunk_turn_count = _reset_context()
                while not frame_queue.empty():
                    try:
                        frame_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
            was_enabled = True

            item = await frame_queue.get()
            frame_count = item["frame_count"]
            faces = item["faces"]
            img_b64 = item["img_b64"]
            t_face = item["t_face"]

            now = time.time()
            real_interval = now - last_process_time
            last_process_time = now

            # ── build user message ──
            elapsed = now - session_start
            user_msg: dict = {
                "role": "user",
                "content": [
                    {"type": "text", "text": f"<{elapsed:.1f} seconds>"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                ],
            }

            # ── chunk reset ──
            if chunk_turn_count >= args.chunk:
                chunk_messages = []
                chunk_turn_count = 0

            chunk_messages.append(user_msg)
            chunk_turn_count += 1

            messages: list[dict] = [
                {"role": "system", "content": system_prompt},
            ]
            messages.extend(chunk_messages)

            if args.debug_print_vlm_messages:
                _print_vlm_messages(messages, args.main_model, generation_kwargs)

            try:
                t0 = time.perf_counter()
                response = await client.chat.completions.create(
                    model=args.main_model,
                    messages=messages,
                    **generation_kwargs,
                )
                raw_text = response.choices[0].message.content or ""
                elapsed_vlm = time.perf_counter() - t0
            except Exception as e:
                print(f"[{ts()}] VLM error: {e}", file=sys.stderr)
                continue

            generated = normalize_output(raw_text)
            chunk_messages.append({"role": "assistant", "content": generated})

            if generated.startswith("</response>"):
                events = parse_actions(generated)
                labels = ", ".join(f"[{e['action_type']}] {e['subject']}" for e in events)
                print(f"\n[{ts()}] \033[91m[ACTION] {labels} (face={t_face:.2f}s vlm={elapsed_vlm:.2f}s interval={real_interval:.2f}s)\033[0m")
                asyncio.create_task(
                    post_action_events(
                        events,
                        args.action_event_host, args.action_event_port,
                    )
                )
            elif args.show_silence:
                print(f".(face={t_face:.2f}s vlm={elapsed_vlm:.2f}s interval={real_interval:.2f}s)", end="", flush=True)

    try:
        await asyncio.gather(produce_frames(), consume_frames())
    except (KeyboardInterrupt, asyncio.CancelledError):
        print(f"\n[{ts()}] Stopped.")
    finally:
        cap.release()
        await control_runner.cleanup()


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def main():
    parser = argparse.ArgumentParser(description="Action Event Detection")
    parser.add_argument("--source", choices=("camera", "foxglove"), default=_env("VIDEO_SOURCE", "camera"))
    parser.add_argument("--camera", type=int, default=int(_env("CAMERA_ID", "0")))
    parser.add_argument("--width", type=int, default=int(_env("CAMERA_WIDTH", "0")))
    parser.add_argument("--height", type=int, default=int(_env("CAMERA_HEIGHT", "0")))
    parser.add_argument("--foxglove-topic", default=_env("FOXGLOVE_TOPIC", "/camera/image/compressed"))
    parser.add_argument("--foxglove-url", default=_env("FOXGLOVE_BRIDGE_URL", ""))
    parser.add_argument("--chunk", type=int, default=int(_env("CHUNK", "100")))
    parser.add_argument("--control-port", type=int, default=int(_env("CONTROL_PORT", "8771")), help="Control API listen port")
    parser.add_argument("--show-silence", action="store_true")
    parser.add_argument("--save-annotated", action="store_true", default=_env_bool("SAVE_ANNOTATED"), help="Save annotated frames to disk")
    parser.add_argument("--save-dir", default=_env("SAVE_ANNOTATED_DIR", "annotated_frames"), help="Directory for saved annotated frames")
    parser.add_argument("--debug-print-vlm-messages", action="store_true", default=_env_bool("DEBUG_PRINT_VLM_MESSAGES"), help="Print full VLM messages before each call")

    parser.add_argument("--main-api-base", default=_env("MAIN_API_BASE", "http://127.0.0.1:7060/v1"))
    parser.add_argument("--main-model", default=_env("MAIN_MODEL", "streamingharness-8b"))
    parser.add_argument("--api-key", default=_env("MODEL_API_KEY", "EMPTY"))
    parser.add_argument("--max-tokens", type=int, default=int(_env("MAIN_MAX_TOKENS", "256")))
    parser.add_argument("--temperature", type=float, default=float(_env("MAIN_TEMPERATURE", "0.8")))
    parser.add_argument("--top-p", type=float, default=float(_env("MAIN_TOP_P", "0.9")))
    parser.add_argument("--top-k", type=int, default=int(_env("MAIN_TOP_K", "40")))
    parser.add_argument("--repetition-penalty", type=float, default=float(_env("MAIN_REPETITION_PENALTY", "1.0")))
    parser.add_argument("--presence-penalty", type=float, default=float(_env("MAIN_PRESENCE_PENALTY", "0.4")))
    parser.add_argument("--request-timeout", type=float, default=float(_env("REQUEST_TIMEOUT", "300")))

    parser.add_argument("--action-event-host", default=_env("ACTION_EVENT_HOST", "127.0.0.1"))
    parser.add_argument("--action-event-port", type=int, default=int(_env("ACTION_EVENT_PORT", "8770")))

    parser.add_argument("--system-prompt", default=_env("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT))
    args = parser.parse_args()

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()