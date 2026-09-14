#!/usr/bin/env python3
"""
Action Event Detection — 单文件一体化脚本。

摄像头采集 → 远程 vLLM 推理 → 检测到动作时 POST /action_event。

Usage:
  python main.py
  python main.py --camera 1
  python main.py --source foxglove
"""

import asyncio
import base64
import io
import os
import shutil
import sys
import tempfile
import time
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

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
                    elif t in ("image", "image_url", "video", "video_url"):
                        url = str(item.get("image", item.get("image_url", item.get("video", item.get("video_url", {}).get("url", "")))))
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


async def main_async(args: SimpleNamespace) -> None:
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

    # ── producer: capture + face detect → video segments ──
    async def produce_frames():
        frame_count = 0
        last_capture = 0.0
        was_enabled = detection_state.enabled
        segment_frames = int(args.video_fps * args.video_segment_secs)

        buffer: list[np.ndarray] = []
        face_times: list[float] = []
        segment_index = 0
        faces = []

        while True:
            if not detection_state.enabled:
                was_enabled = False
                buffer.clear()
                face_times.clear()
                await asyncio.sleep(0.1)
                continue

            if detection_state.enabled and not was_enabled:
                frame_count = 0
                last_capture = 0.0
                buffer.clear()
                face_times.clear()
                segment_index = 0
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
            is_first = len(buffer) == 0
            is_last = len(buffer) == segment_frames - 1
            do_detect = is_first or is_last

            if do_detect:
                if bgr_input:
                    faces = await detect_faces(frame)
                else:
                    faces = await detect_faces(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR))

            if bgr_input:
                annotated = draw_face_detect(frame, faces)
            else:
                annotated = draw_face_detect(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR), faces)
            t_face = time.perf_counter() - t_face
            if do_detect:
                face_times.append(t_face)

            buffer.append(annotated)

            if len(buffer) >= segment_frames:
                seg_start = segment_index * args.video_segment_secs
                seg_end = seg_start + args.video_segment_secs
                segment_index += 1

                h, w = buffer[0].shape[:2]
                fd, tmp_path = tempfile.mkstemp(suffix=".mp4")
                os.close(fd)

                fourcc = cv2.VideoWriter_fourcc(*"mp4v")
                out = cv2.VideoWriter(tmp_path, fourcc, args.video_fps, (w, h))
                for frm in buffer:
                    out.write(frm)
                out.release()

                with open(tmp_path, "rb") as f:
                    video_b64 = base64.b64encode(f.read()).decode()

                if args.save_video:
                    save_dir = Path(args.save_video_dir)
                    save_dir.mkdir(parents=True, exist_ok=True)
                    save_path = save_dir / f"{session_id}_{segment_index:04d}.mp4"
                    shutil.move(tmp_path, str(save_path))
                else:
                    os.unlink(tmp_path)

                avg_t_face = sum(face_times) / len(face_times)

                await frame_queue.put({
                    "seg_start": seg_start,
                    "seg_end": seg_end,
                    "faces": faces,
                    "video_b64": video_b64,
                    "t_face": avg_t_face,
                })

                buffer.clear()
                face_times.clear()

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
            faces = item["faces"]
            video_b64 = item["video_b64"]
            t_face = item["t_face"]
            seg_start = item["seg_start"]
            seg_end = item["seg_end"]

            now = time.time()
            real_interval = now - last_process_time
            last_process_time = now

            # ── build user message ──
            user_msg: dict = {
                "role": "user",
                "content": [
                    {
                        "type": "video_url",
                        "video_url": {"url": f"data:video/mp4;base64,{video_b64}"},
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
    config = SimpleNamespace(
        source=_env("VIDEO_SOURCE", "camera"),
        camera=int(_env("CAMERA_ID", "0")),
        width=int(_env("CAMERA_WIDTH", "0")),
        height=int(_env("CAMERA_HEIGHT", "0")),
        foxglove_topic=_env("FOXGLOVE_TOPIC", "/camera/image/compressed"),
        foxglove_url=_env("FOXGLOVE_BRIDGE_URL", ""),
        chunk=int(_env("CHUNK", "1")),
        video_segment_secs=float(_env("VIDEO_SEGMENT_SECS", "0.5")),
        video_fps=float(_env("VIDEO_FPS", "20.0")),
        control_port=int(_env("CONTROL_PORT", "8771")),
        show_silence=_env_bool("SHOW_SILENCE"),
        save_video=_env_bool("SAVE_VIDEO"),
        save_video_dir=_env("SAVE_VIDEO_DIR", "video_segments"),
        debug_print_vlm_messages=_env_bool("DEBUG_PRINT_VLM_MESSAGES"),
        main_api_base=_env("MAIN_API_BASE", "http://127.0.0.1:7060/v1"),
        main_model=_env("MAIN_MODEL", "streamingharness-8b"),
        api_key=_env("MODEL_API_KEY", "EMPTY"),
        max_tokens=int(_env("MAIN_MAX_TOKENS", "256")),
        temperature=float(_env("MAIN_TEMPERATURE", "0.8")),
        top_p=float(_env("MAIN_TOP_P", "0.9")),
        top_k=int(_env("MAIN_TOP_K", "40")),
        repetition_penalty=float(_env("MAIN_REPETITION_PENALTY", "1.0")),
        presence_penalty=float(_env("MAIN_PRESENCE_PENALTY", "0.4")),
        request_timeout=float(_env("REQUEST_TIMEOUT", "300")),
        action_event_host=_env("ACTION_EVENT_HOST", "127.0.0.1"),
        action_event_port=int(_env("ACTION_EVENT_PORT", "8770")),
        system_prompt=_env("SYSTEM_PROMPT", DEFAULT_SYSTEM_PROMPT),
    )
    asyncio.run(main_async(config))


if __name__ == "__main__":
    main()