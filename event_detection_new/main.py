#!/usr/bin/env python3
"""
Event Detection System — unified entry point.

Reads all config from .env, starts the inference adapter + video capture
in a single process. No CLI args needed (except --display for live preview).

Usage:
  python main.py
  python main.py --display
"""

import argparse
import asyncio
import base64
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import aiohttp
from dotenv import load_dotenv
from openai import OpenAI
from PIL import Image

# Load .env before other local imports (some modules read env at import time)
load_dotenv(Path(__file__).resolve().parent / ".env")

from webinfer.live_adapter import AdapterConfig, create_app, extract_event, is_silence_output
from aiohttp import web


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "y", "on")


def _env_json_dict(name: str, default: dict) -> dict:
    v = os.getenv(name)
    if not v:
        return dict(default)
    try:
        parsed = json.loads(v)
        if isinstance(parsed, dict):
            return {str(k): float(val) for k, val in parsed.items()}
    except (ValueError, TypeError):
        pass
    return dict(default)


# ---------------------------------------------------------------------------
# adapter config from .env
# ---------------------------------------------------------------------------

def build_adapter_config() -> AdapterConfig:
    """Build AdapterConfig purely from environment variables."""
    summarizer_model = _env("SUMMARIZER_MODEL", "Qwen3.5-2B")
    summarizer_api_base = _env("SUMMARIZER_API_BASE", "http://127.0.0.1:8065/v1")
    chunk = _env_int("CHUNK", 60)
    async_summary_lead_frames = _env_int("ASYNC_SUMMARY_LEAD_FRAMES", 5)
    if async_summary_lead_frames < 0:
        raise ValueError("ASYNC_SUMMARY_LEAD_FRAMES must be >= 0")
    if chunk > 0 and async_summary_lead_frames >= chunk:
        raise ValueError(
            "ASYNC_SUMMARY_LEAD_FRAMES must be smaller than CHUNK "
            f"(got lead={async_summary_lead_frames}, chunk={chunk})"
        )

    return AdapterConfig(
        host="127.0.0.1",
        port=_env_int("ADAPTER_PORT", 8771),
        adapter_model="streaming-infer-adapter",
        api_key=_env("MODEL_API_KEY", "EMPTY"),
        main_api_base=_env("MAIN_API_BASE", "http://127.0.0.1:7060/v1"),
        main_model=_env("MAIN_MODEL", "streamingharness-8b"),
        frame_seconds=_env_float("FRAME_SECONDS", 0.3),
        # 主 VLM 图片分辨率上限（像素数）；0 = 发送原始分辨率
        max_pixels=_env_int("MAX_PIXELS", 262144),
        chunk=chunk,
        compress_every_n_chunks=_env_int("COMPRESS_EVERY_N_CHUNKS", 3),
        async_summary_lead_frames=async_summary_lead_frames,
        enable_summarizer=_env_bool("ENABLE_SUMMARIZER", True),
        summarizer_model=summarizer_model,
        summarizer_api_base=summarizer_api_base,
        longterm_model=summarizer_model,
        longterm_api_base=summarizer_api_base,
        main_max_tokens=_env_int("MAIN_MAX_TOKENS", 256),
        main_temperature=_env_float("MAIN_TEMPERATURE", 0.2),
        main_top_p=_env_float("MAIN_TOP_P", 0.9),
        main_top_k=_env_int("MAIN_TOP_K", 40),
        mid_term_max_tokens=_env_int("MID_TERM_MAX_TOKENS", 4000),
        mid_term_target_tokens=_env_int("MID_TERM_TARGET_TOKEN_COUNT", 1500),
        mid_term_temperature=_env_float("MID_TERM_TEMPERATURE", 0.8),
        long_term_max_tokens=_env_int("LONG_TERM_MAX_TOKENS", 4000),
        long_term_target_tokens=_env_int("LONG_TERM_TARGET_TOKEN_COUNT", 1000),
        long_term_temperature=_env_float("LONG_TERM_TEMPERATURE", 0.3),
        long_term_memory_window=_env_int("LONG_TERM_MEMORY_WINDOW", 5),
        force_silence_before_query=_env_bool("FORCE_SILENCE_BEFORE_QUERY", False),
        debug_print_vlm_messages=_env_bool("DEBUG_PRINT_VLM_MESSAGES", False),
        normalize_output=_env_bool("NORMALIZE_OUTPUT", True),
        main_disable_thinking=_env_bool("MAIN_DISABLE_THINKING", False),
        summary_disable_thinking=_env_bool("SUMMARY_DISABLE_THINKING", True),
        system_prompt="",  # will be resolved by _resolve_system_prompt
        system_event_url=_env("SYSTEM_EVENT_URL", "http://127.0.0.1:8770/system_event"),
        agent_webhook_url=_env("AGENT_WEBHOOK_URL", "http://192.168.217.100:8769/event"),
        system_event_labels=_env("SYSTEM_EVENT_LABELS", ""),
        event_dedup_seconds=5.0,
        time_api_url=_env("TIME_API_URL", ""),
        night_time_start=_env_int("NIGHT_TIME_START", 22),
        night_time_end=_env_int("NIGHT_TIME_END", 6),
        event_confirm_enabled=_env_bool("EVENT_CONFIRM_ENABLED", True),
        event_confirm_seconds=_env_json_dict("EVENT_CONFIRM_SECONDS", {}),
        langfuse_chunk_record_enabled=_env_bool("LANGFUSE_CHUNK_RECORD_ENABLED", True),
        # Live save disabled by default for deployment simplicity
        per_session_dirs=False,
        save_model_inputs=False,
        summarizer_debug=False,
    )


# ---------------------------------------------------------------------------
# adapter runner (background thread)
# ---------------------------------------------------------------------------

def _run_adapter(config: AdapterConfig, ready_event: threading.Event):
    """Run the aiohttp adapter in a background thread. Sets ready_event when listening."""
    app = create_app(config)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    # Python 3.14+: add_signal_handler only works in the main thread, so we
    # disable aiohttp's signal handling here — the main thread already handles
    # KeyboardInterrupt and tears down the process.
    runner = web.AppRunner(app, handle_signals=False)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, config.host, config.port)
    loop.run_until_complete(site.start())

    ready_event.set()

    try:
        loop.run_forever()
    finally:
        loop.run_until_complete(runner.cleanup())
        loop.close()


# ---------------------------------------------------------------------------
# detection subprocess (MiniCPM-o event detection, sibling project)
# ---------------------------------------------------------------------------

def _spawn_detection() -> subprocess.Popen | None:
    """Launch detection/main.py as a subprocess. Returns None if disabled."""
    if not _env_bool("DETECTION_ENABLED", False):
        return None

    detection_dir = Path(__file__).resolve().parent / "detection"
    cmd = [sys.executable, "main.py"]

    source = _env("DETECTION_SOURCE", "foxglove").strip().lower()
    if source == "foxglove":
        cmd += ["--source", "foxglove"]
        cmd += [
            "--foxglove-topic",
            _env("DETECTION_FOXGLOVE_TOPIC",
                 _env("FOXGLOVE_TOPIC",
                      "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed")),
        ]
        url = _env("DETECTION_FOXGLOVE_URL", _env("FOXGLOVE_BRIDGE_URL", "")).strip()
        if url:
            cmd += ["--foxglove-url", url]
    elif source == "camera":
        cmd += ["--source", "local", "--camera", str(_env_int("DETECTION_CAMERA_ID", 0))]
    elif source == "zenoh":
        cmd += ["--source", "zenoh"]
        cmd += [
            "--zenoh-topic",
            _env("DETECTION_ZENOH_TOPIC", _env("ZENOH_TOPIC", "camera/annotated")),
        ]
        url = _env("DETECTION_ZENOH_URL", _env("ZENOH_URL", "")).strip()
        if url:
            cmd += ["--zenoh-url", url]
        cmd += ["--zenoh-fps", str(_env_float("DETECTION_ZENOH_FPS", _env_float("ZENOH_CLIENT_FPS", 2.0)))]

    cmd += ["--fps", str(_env_float("DETECTION_FPS", 2.0))]

    if _env_bool("DETECTION_NO_AUDIO", False):
        cmd += ["--no-audio-capture"]
    mic = _env("DETECTION_MIC_NAME", "").strip()
    if mic:
        cmd += ["--mic-name", mic]
    host = _env("DETECTION_MINICPMO_HOST", "").strip()
    if host:
        cmd += ["--minicpmo-host", host]
    if _env_bool("DETECTION_INSECURE", False):
        cmd += ["--insecure"]
    if _env_bool("DEBUG", False):
        cmd += ["--debug"]

    return subprocess.Popen(cmd, cwd=str(detection_dir))


# ---------------------------------------------------------------------------
# video capture helpers
# ---------------------------------------------------------------------------

def _encode_frame_jpeg(frame, bgr_input: bool = True) -> bytes:
    """Convert a numpy frame to JPEG bytes."""
    if bgr_input:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(frame)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return buf.getvalue()


def _encode_frame_b64(frame, bgr_input: bool = True) -> str:
    """Convert numpy frame to base64 JPEG string."""
    return base64.b64encode(_encode_frame_jpeg(frame, bgr_input)).decode("utf-8")


def _create_camera(camera_id: int, width: int, height: int):
    cap = cv2.VideoCapture(camera_id)
    if not cap.isOpened():
        print(f"Error: cannot open camera {camera_id}", file=sys.stderr)
        sys.exit(1)
    if width > 0:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
    if height > 0:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
    return cap, True  # (capture, bgr_input)


def _create_foxglove(topic: str, url: str):
    from foxglove_client import FoxgloveImageCapture
    cap = FoxgloveImageCapture(topic=topic, url=url or None)
    return cap, False  # (capture, bgr_input) — Foxglove returns RGB


def _create_rosbridge(topic: str, url: str):
    from foxglove_client import RosbridgeImageCapture
    cap = RosbridgeImageCapture(topic=topic, url=url or None)
    return cap, False  # (capture, bgr_input) — rosbridge returns RGB


def _create_video_source():
    """Create the configured capture source and describe it for logs/UI."""
    source = _env("VIDEO_SOURCE", "foxglove").strip().lower()
    if source == "foxglove":
        topic = _env("FOXGLOVE_TOPIC", "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed")
        url = _env("FOXGLOVE_BRIDGE_URL", "ws://192.168.217.100:8768")
        cap, bgr_input = _create_foxglove(topic, url)
        return cap, bgr_input, f"Foxglove topic={topic}"
    if source == "rosbridge":
        from foxglove_client import ROSBRIDGE_URL
        topic = _env("FOXGLOVE_TOPIC", "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed")
        url = _env("ROSBRIDGE_URL", ROSBRIDGE_URL)
        cap, bgr_input = _create_rosbridge(topic, url)
        return cap, bgr_input, f"ROS1 rosbridge topic={topic}"
    if source == "zenoh":
        from zenoh_client import DEFAULT_TOPIC, ZenohImageCapture

        topic = _env("ZENOH_TOPIC", DEFAULT_TOPIC)
        url = _env("ZENOH_URL", "tcp/192.168.1.100:7450")
        fps = _env_float("ZENOH_CLIENT_FPS", 2.0)
        cap = ZenohImageCapture(topic=topic, url=url or None, fps=fps)
        return cap, False, f"Zenoh topic={topic} fps={fps:g}"
    if source == "camera":
        camera_id = _env_int("CAMERA_ID", 0)
        width = _env_int("CAMERA_WIDTH", 0)
        height = _env_int("CAMERA_HEIGHT", 0)
        cap, bgr_input = _create_camera(camera_id, width, height)
        return cap, bgr_input, f"Camera {camera_id}"
    raise ValueError(f"Unsupported VIDEO_SOURCE: {source!r}")


def _read_capture_frame(cap, source: str):
    """Read one frame using the timeout required by network capture sources."""
    if source == "camera":
        return cap.read()
    return cap.read(timeout=2.0 if source == "zenoh" else 1.0)


def _handle_text(text: str, show_silence: bool) -> str:
    text = (text or "").strip()
    # 事件：JSON {"event":...} 或标签 </response>/<response>
    ev = extract_event(text)
    if ev:
        label, desc, name = ev
        line = f"[{ts()}] ** {label} {desc}".rstrip()
        if name:
            line += f" [{name}]"
        print(f"\n{line}", flush=True)
        return desc or "response"
    # 无事件：JSON {"event": null} 或标签 </silence>/<silence>
    if is_silence_output(text):
        if show_silence:
            print(".", end="", flush=True)
        return "silence"
    print(f"\n[{ts()}] {text}")
    return text or "empty"


def _draw_overlay(frame, source_desc: str, sent_count: int, last_status: str) -> None:
    lines = [
        source_desc,
        f"sent frames: {sent_count}",
        f"last: {last_status}",
        "q/Esc: quit",
    ]
    x, y = 12, 24
    line_height = 24
    max_width = max(cv2.getTextSize(line, cv2.FONT_HERSHEY_SIMPLEX, 0.58, 1)[0][0] for line in lines)
    box_height = line_height * len(lines) + 12
    overlay = frame.copy()
    cv2.rectangle(overlay, (6, 6), (max_width + 24, box_height), (0, 0, 0), -1)
    cv2.addWeighted(overlay, 0.6, frame, 0.4, 0.0, frame)
    for line in lines:
        cv2.putText(frame, line, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (255, 255, 255), 1, cv2.LINE_AA)
        y += line_height


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Event Detection — unified adapter + capture")
    parser.add_argument("--display", action="store_true",
                        default=_env_bool("CAPTURE_DISPLAY", False),
                        help="Show live preview window")
    parser.add_argument("--show-silence", action="store_true",
                        help="Print a dot for each silence frame")
    parser.add_argument("--no-detection", action="store_true",
                        help="Disable the MiniCPM-o detection subprocess")
    args = parser.parse_args()

    # ---------- adapter config ----------
    config = build_adapter_config()
    adapter_url = f"http://{config.host}:{config.port}/v1"

    # ---------- video source ----------
    source = _env("VIDEO_SOURCE", "foxglove").strip().lower()
    cap, bgr_input, source_desc = _create_video_source()

    interval = _env_float("CAPTURE_INTERVAL", 0.3)
    dashboard_interval = max(0.03, _env_float("DASHBOARD_FRAME_INTERVAL", 0.1))
    session_id = uuid.uuid4().hex[:12]

    # ---------- display ----------
    display_enabled = args.display
    display_window = _env("CAPTURE_WINDOW_TITLE", "event_detection")
    display_scale = _env_float("CAPTURE_DISPLAY_SCALE", 1.0)

    if display_enabled and sys.platform.startswith("linux"):
        if not (os.getenv("DISPLAY") or os.getenv("WAYLAND_DISPLAY")):
            print(f"[{ts()}] Display disabled: no GUI display found.", file=sys.stderr)
            display_enabled = False

    # ---------- start adapter in background ----------
    print(f"[{ts()}] Starting adapter on {adapter_url} ...")
    print(f"[{ts()}] Main model: {config.main_model} @ {config.main_api_base}")
    print(f"[{ts()}] Chunk={config.chunk}  Async summary lead={config.async_summary_lead_frames}  "
          f"Frame interval={config.frame_seconds}s  "
          f"Summarizer={'on' if config.enable_summarizer else 'off'}  "
          f"Temperature={config.main_temperature}")
    print(f"[{ts()}] Night: {config.night_time_start}:00-{config.night_time_end}:00  "
          f"Time API: {config.time_api_url or 'local'}")

    ready = threading.Event()
    adapter_thread = threading.Thread(target=_run_adapter, args=(config, ready), daemon=True)
    adapter_thread.start()

    # Wait for adapter health check
    deadline = time.time() + 30
    while not ready.is_set() and time.time() < deadline:
        time.sleep(0.1)
    if not ready.is_set():
        print(f"[{ts()}] Error: adapter did not start within 30s", file=sys.stderr)
        sys.exit(1)

    # Give it a moment to fully initialize
    time.sleep(1)
    print(f"[{ts()}] Adapter ready.")

    # ---------- capture / inference (decoupled producer-consumer) ----------
    client = OpenAI(base_url=adapter_url, api_key="EMPTY", timeout=300)
    cap_capacity = _env_int("CAPTURE_BUFFER_FRAMES", 2)

    class _LatestBuffer:
        """线程安全缓冲,只保留最新的 capacity 帧,满了自动丢最旧(不堆积)."""
        def __init__(self, capacity: int):
            self._items = deque(maxlen=capacity)
            self._lock = threading.Lock()

        def push(self, item):
            with self._lock:
                self._items.append(item)

        def pop_latest(self):
            with self._lock:
                return self._items.pop() if self._items else None

    frames = _LatestBuffer(cap_capacity)
    dashboard_frames = _LatestBuffer(1)
    stop = threading.Event()
    dashboard_frame_url = _env("DASHBOARD_FRAME_URL", "http://127.0.0.1:8770/frame").strip()

    # 跨线程共享:推理结果状态(采集线程显示用)
    status_lock = threading.Lock()
    last_status = "waiting"

    def _set_status(status: str) -> None:
        nonlocal last_status
        with status_lock:
            last_status = status

    def _get_status() -> str:
        with status_lock:
            return last_status

    print(f"[{ts()}] Session: {session_id}")
    print(f"[{ts()}] Source: {source_desc}")
    print(f"[{ts()}] Interval: {interval}s  capture buffer keeps latest {cap_capacity} frame(s)")
    print("-" * 50)

    def _to_display_bgr(frame):
        return frame.copy() if bgr_input else cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)

    def _scale(frame, scale):
        if scale <= 0 or abs(scale - 1.0) < 1e-6:
            return frame
        h, w = frame.shape[:2]
        return cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))),
                          interpolation=cv2.INTER_AREA)

    def _capture_producer():
        """生产者线程:按 interval 抓帧 -> 显示 -> 缓存进缓冲(丢最旧),绝不阻塞在推理上."""
        nonlocal display_enabled
        local_frame_count = 0
        last_inference_capture = 0.0
        last_dashboard_capture = 0.0
        while not stop.is_set():
            ret, frame = _read_capture_frame(cap, source)

            if not ret or frame is None:
                print(f"[{ts()}] Frame read failed, retrying...", file=sys.stderr)
                time.sleep(0.5)
                continue

            if display_enabled:
                try:
                    df = _scale(_to_display_bgr(frame), display_scale)
                    _draw_overlay(df, source_desc, local_frame_count, _get_status())
                    cv2.imshow(display_window, df)
                    key = cv2.waitKey(1) & 0xFF
                    if key in (27, ord("q")):
                        print(f"\n[{ts()}] Preview closed by user.")
                        stop.set()
                        break
                except cv2.error:
                    display_enabled = False

            # 页面展示和推理使用独立节拍。二者都只保留最新帧，不产生延迟堆积。
            now = time.monotonic()
            if dashboard_frame_url and now - last_dashboard_capture >= dashboard_interval:
                dashboard_frames.push(frame)
                last_dashboard_capture = now

            if now - last_inference_capture >= interval:
                local_frame_count += 1
                captured_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
                frames.push((frame, captured_at))  # 时间绑定采集瞬间；满了丢最旧
                last_inference_capture = now

            time.sleep(0.001)

    def _dashboard_uploader():
        """复用 HTTP 连接上传展示帧；服务不可用时直接丢帧。"""
        async def _upload_loop():
            last_error_log = 0.0
            timeout = aiohttp.ClientTimeout(total=2)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                while not stop.is_set():
                    frame = dashboard_frames.pop_latest()
                    if frame is None:
                        await asyncio.sleep(0.01)
                        continue
                    try:
                        jpeg = _encode_frame_jpeg(frame, bgr_input=bgr_input)
                        headers = {
                            "Content-Type": "image/jpeg",
                            "X-Frame-Timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                        }
                        async with session.post(dashboard_frame_url, data=jpeg, headers=headers) as response:
                            await response.read()
                            if response.status >= 400:
                                raise RuntimeError(f"HTTP {response.status}")
                    except (aiohttp.ClientError, asyncio.TimeoutError, OSError, RuntimeError) as exc:
                        now = time.monotonic()
                        if now - last_error_log >= 30:
                            print(f"[{ts()}] Dashboard frame upload unavailable: {exc}", file=sys.stderr)
                            last_error_log = now

        asyncio.run(_upload_loop())

    producer = threading.Thread(target=_capture_producer, daemon=True)
    producer.start()
    dashboard_uploader = None
    if dashboard_frame_url:
        print(f"[{ts()}] Dashboard frames: {dashboard_frame_url} ({1 / dashboard_interval:.1f} FPS target)")
        dashboard_uploader = threading.Thread(target=_dashboard_uploader, daemon=True)
        dashboard_uploader.start()
    consumer_frame_count = 0

    # ---------- detection subprocess (MiniCPM-o) ----------
    detection_proc: subprocess.Popen | None = None

    def _detection_supervisor():
        nonlocal detection_proc
        restart_delay = 5
        while not stop.is_set():
            try:
                proc = _spawn_detection()
            except Exception as e:
                print(f"[{ts()}] Failed to start detection subprocess: {e}", file=sys.stderr)
                return
            if proc is None:
                return
            detection_proc = proc
            print(f"[{ts()}] Detection subprocess started (pid={proc.pid})")
            proc.wait()
            if stop.is_set():
                break
            print(f"[{ts()}] Detection subprocess exited (code={proc.returncode}); "
                  f"restarting in {restart_delay}s")
            time.sleep(restart_delay)

    detection_thread = None
    if _env_bool("DETECTION_ENABLED", False) and not args.no_detection:
        detection_thread = threading.Thread(target=_detection_supervisor, daemon=True)
        detection_thread.start()

    try:
        while not stop.is_set():
            frame_item = frames.pop_latest()
            if frame_item is None:
                time.sleep(0.01)
                continue
            frame, captured_at = frame_item
            consumer_frame_count += 1
            try:
                img_b64 = _encode_frame_b64(frame, bgr_input=bgr_input)
                response = client.chat.completions.create(
                    model="streaming-infer-adapter",
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "image_url",
                             "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
                        ]
                    }],
                    max_tokens=256,
                    temperature=0.7,
                    extra_headers={
                        "x-streaming-session": session_id,
                        "x-frame-captured-at": captured_at,
                    },
                )
                _set_status(_handle_text(
                    (response.choices[0].message.content or "").strip(),
                    args.show_silence,
                ))
            except Exception as e:
                _set_status(f"API error: {e}")
                print(f"[{ts()}] API error: {e}", file=sys.stderr)

    except KeyboardInterrupt:
        print(f"\n[{ts()}] Stopped. {consumer_frame_count} frames processed.")
    finally:
        stop.set()
        producer.join(timeout=2)
        if dashboard_uploader is not None:
            dashboard_uploader.join(timeout=2)
        if detection_thread is not None:
            detection_thread.join(timeout=2)
        if detection_proc is not None and detection_proc.poll() is None:
            detection_proc.terminate()
            try:
                detection_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                detection_proc.kill()
        cap.release()
        if display_enabled:
            try:
                cv2.destroyAllWindows()
            except cv2.error:
                pass


if __name__ == "__main__":
    main()
