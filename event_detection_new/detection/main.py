#!/usr/bin/env python3
"""
MiniCPM-o Realtime API — 视频双工事件检测。

摄像头采集 → 人脸检测标注 → MiniCPM-o WebSocket → 文本打印到控制台。

Usage:
  python main.py
  python main.py --camera 1
  python main.py --source foxglove
  python main.py --no-audio-capture
"""

import argparse
import asyncio
import json
import os
import queue
import sys
import threading
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pyaudio
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

from face_detect import _get_ident_client, draw_face_detect, detect_faces
from minicpmo_client import MiniCPMOClient


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, default)


def _env_bool(key: str, default: bool = False) -> bool:
    val = os.getenv(key, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def _env_float(key: str, default: float) -> float:
    val = os.getenv(key, "")
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# event mapping + outbound push (SYSTEM_EVENT_URL / AGENT_WEBHOOK_URL)
# ---------------------------------------------------------------------------

# 与 prompts.py 输出格式一一对应。text 中包含任一 keyword 即判定命中。
EVENT_RULES = [
    ("cough", "有人咳嗽", ["咳嗽"]),
    ("knock", "有人敲门", ["敲门"]),
    ("person_appear", "有人出现", ["有人出现", "突然出现"]),
]

EVENT_DEDUP_SECONDS = float(os.getenv("EVENT_DEDUP_SECONDS", "5"))
_last_event_push: dict[str, float] = {}


def _match_event(text: str) -> tuple[str, str] | None:
    for label, desc, keywords in EVENT_RULES:
        if any(kw in text for kw in keywords):
            return label, desc
    return None


def _http_push_event(label: str, desc: str) -> None:
    """同步推送单个事件（urllib，调用方放入线程池）。"""
    system_url = os.getenv("SYSTEM_EVENT_URL", "").strip()
    agent_url = os.getenv("AGENT_WEBHOOK_URL", "").strip()
    headers = {"Content-Type": "application/json"}

    if system_url:
        payload = {
            "event": label,
            "description": desc,
            "level": "alert",
            "name": "",
            "images": [],
            "videos": [],
        }
        try:
            req = urllib.request.Request(system_url, data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status >= 400:
                    print(f"[{ts()}] Push system event failed: HTTP {resp.status}", file=sys.stderr)
        except Exception as e:
            print(f"[{ts()}] Push system event failed: {e}", file=sys.stderr)

    if agent_url:
        payload = {
            "event_type": label,
            "event_name": label,
            "description": desc,
            "name": "",
            "image_base64": "",
            "image_mime_type": "image/jpeg",
            "video_base64": "",
            "video_mime_type": "video/avi",
        }
        try:
            req = urllib.request.Request(agent_url, data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status >= 400:
                    print(f"[{ts()}] Push agent webhook failed: HTTP {resp.status}", file=sys.stderr)
        except Exception as e:
            print(f"[{ts()}] Push agent webhook failed: {e}", file=sys.stderr)


def _emit_event(loop, text: str) -> None:
    ev = _match_event(text)
    if not ev:
        return
    label, desc = ev
    now = time.time()
    if now - _last_event_push.get(label, 0) < EVENT_DEDUP_SECONDS:
        return
    _last_event_push[label] = now
    print(f"\n[{ts()}] ** [EVENT] {desc}", flush=True)
    loop.run_in_executor(None, _http_push_event, label, desc)


async def main_async(args: argparse.Namespace) -> None:
    # ── init camera ──
    if args.source == "foxglove":
        from foxglove_client import FoxgloveImageCapture

        cap = FoxgloveImageCapture(topic=args.foxglove_topic, url=args.foxglove_url or None)
        bgr_input = False
        source_desc = f"Foxglove topic={args.foxglove_topic}"
    elif args.source == "zenoh":
        from zenoh_client import ZenohImageCapture

        cap = ZenohImageCapture(topic=args.zenoh_topic, url=args.zenoh_url or None, fps=args.zenoh_fps)
        bgr_input = False
        source_desc = f"Zenoh topic={args.zenoh_topic} via {args.zenoh_url or 'LAN multicast'}"
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

    # ── fetch known face names ──
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

    # ── audio capture (optional) ──
    audio_capture = None
    audio_queue: queue.Queue | None = None

    if not args.no_audio_capture:
        from config import MIC_DEVICE_NAME
        from scipy.signal import resample_poly

        audio_queue = queue.Queue(maxsize=80)
        mic_name = args.mic_name or MIC_DEVICE_NAME or None
        TARGET_RATE = 16000

        class SimpleAudioCapture:
            def __init__(self, device_name: str | None = None):
                # suppress ALSA/JACK probe noise
                _stderr_fd = os.dup(2)
                _null_fd = os.open(os.devnull, os.O_WRONLY)
                os.dup2(_null_fd, 2)
                try:
                    self._p = pyaudio.PyAudio()
                finally:
                    os.dup2(_stderr_fd, 2)
                    os.close(_null_fd)
                    os.close(_stderr_fd)
                self._stream = None
                self._running = False
                self._need_resample = False
                self.device_name = device_name or "default"
                self.device_rate = TARGET_RATE
                self._device_index = None

                def _use_device(idx: int) -> None:
                    info = self._p.get_device_info_by_index(idx)
                    self._device_index = idx
                    self.device_name = info["name"]
                    self.device_rate = int(info["defaultSampleRate"])

                if device_name and device_name != "default":
                    idx = self._find_device_index(device_name)
                    if idx is not None:
                        _use_device(idx)
                if self._device_index is None:
                    # 找不到指定设备时自动回退到系统默认输入设备
                    try:
                        _use_device(self._p.get_default_input_device_info()["index"])
                    except OSError:
                        self._device_index = None

                if self.device_rate != TARGET_RATE:
                    self._need_resample = True
                self._chunk_size = int(1600 * self.device_rate / TARGET_RATE)

            def _find_device_index(self, keyword: str) -> int | None:
                kw = keyword.lower()
                for i in range(self._p.get_device_count()):
                    info = self._p.get_device_info_by_index(i)
                    if info["maxInputChannels"] > 0 and kw in info["name"].lower():
                        return i
                return None

            def start(self, q: queue.Queue) -> bool:
                if self._device_index is None:
                    return False
                try:
                    self._stream = self._p.open(
                        format=pyaudio.paInt16,
                        channels=1,
                        rate=self.device_rate,
                        input=True,
                        input_device_index=self._device_index,
                        frames_per_buffer=self._chunk_size,
                        stream_callback=lambda in_data, frame_count, time_info, status: (
                            self._handle_audio(in_data, q), pyaudio.paContinue
                        ),
                    )
                    self._stream.start_stream()
                    self._running = True
                    return True
                except OSError:
                    return False

            def _handle_audio(self, in_data: bytes, q: queue.Queue):
                if q.full():
                    return
                audio_np = np.frombuffer(in_data, dtype=np.int16).astype(np.float32) / 32768.0
                if self._need_resample:
                    audio_np = resample_poly(audio_np, TARGET_RATE, self.device_rate).astype(np.float32)
                q.put(audio_np.copy())

            def stop(self):
                self._running = False
                if self._stream:
                    self._stream.stop_stream()
                    self._stream.close()
                self._p.terminate()

        audio_capture = SimpleAudioCapture(device_name=mic_name)
        if audio_capture.start(audio_queue):
            print(f"[{ts()}] Audio capture started (mic: {audio_capture.device_name}, "
                  f"{audio_capture.device_rate}Hz → {TARGET_RATE}Hz)")
        else:
            print(f"[{ts()}] Audio capture unavailable, continuing without audio")
            audio_capture.stop()
            audio_capture = None
            audio_queue = None

    # ── capture thread: camera + face detect ──
    from config import VIDEO_COMPRESS_TARGET_SIZE

    capture_queue: queue.Queue = queue.Queue(maxsize=2)
    _stop_capture = threading.Event()

    def _capture_thread():
        frame_count = 0
        interval = 1.0 / args.fps
        is_camera = args.source == "local"

        while not _stop_capture.is_set():
            t_start = time.perf_counter()

            if is_camera:
                ret, frame = cap.read()
            else:
                ret, frame = cap.read(timeout=1.0)

            if not ret or frame is None:
                time.sleep(0.5)
                continue

            tw, th = VIDEO_COMPRESS_TARGET_SIZE
            if frame.shape[1] != tw or frame.shape[0] != th:
                frame = cv2.resize(frame, (tw, th), interpolation=cv2.INTER_AREA)

            frame_bgr = frame if bgr_input else cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
            if frame_count % 2 == 0:
                faces = detect_faces(frame_bgr)
                annotated = draw_face_detect(frame_bgr, faces)
            else:
                annotated = frame_bgr

            frame_count += 1

            item = {"annotated": annotated, "frame_count": frame_count}
            while True:
                try:
                    capture_queue.put_nowait(item)
                    break
                except queue.Full:
                    try:
                        capture_queue.get_nowait()
                    except queue.Empty:
                        pass

            elapsed = time.perf_counter() - t_start
            if elapsed < interval:
                time.sleep(interval - elapsed)

    capture_thread = threading.Thread(target=_capture_thread, daemon=True)
    capture_thread.start()

    # ── CPM session loop ──
    retry_delay = 2
    max_retry_delay = 60

    while not _stop_capture.is_set():
        from config import MINICPMO_HOST, MINICPMO_MODE, MINICPMO_SEND_INTERVAL, MINICPMO_SESSION_DURATION
        from prompts import CPM_SYSTEM_PROMPT

        system_prompt = CPM_SYSTEM_PROMPT.format(
            known_names=", ".join(known_names) if known_names else "暂无"
        )

        session_config = {
            "system_prompt": system_prompt,
            "config": {
                "max_tokens": 300,
                "temperature": 0.3,
            },
        }
        client = MiniCPMOClient(
            host=MINICPMO_HOST,
            mode=MINICPMO_MODE,
            session_config=session_config,
            insecure=args.insecure,
        )

        try:
            print(f"[{ts()}] Connecting to MiniCPM-o: {MINICPMO_HOST} (mode={MINICPMO_MODE})")
            await client.connect()
            print(f"[{ts()}] Session: {client.session_id}")
            print(f"[{ts()}] Source: {source_desc}")
            print("-" * 50)
            retry_delay = 2

            # ── send loop ──
            AUDIO_CHUNK_SAMPLES = 16000

            async def send_loop():
                frame_index = 0
                next_tick = time.time() + MINICPMO_SEND_INTERVAL
                send_task: asyncio.Task | None = None
                audio_buffer = np.array([], dtype=np.float32)

                # drain stale queues on new session
                if audio_queue is not None:
                    for _ in range(80):
                        try:
                            audio_queue.get_nowait()
                        except queue.Empty:
                            break
                for _ in range(120):
                    try:
                        capture_queue.get_nowait()
                    except queue.Empty:
                        break

                while True:
                    await asyncio.sleep(0.05)

                    if client.closed_event.is_set():
                        break

                    now_t = time.time()
                    if now_t < next_tick:
                        continue
                    if now_t > next_tick + MINICPMO_SEND_INTERVAL:
                        next_tick = now_t + MINICPMO_SEND_INTERVAL
                    else:
                        next_tick += MINICPMO_SEND_INTERVAL

                    if send_task is not None:
                        if not send_task.done():
                            if args.debug:
                                print(f"[{ts()}] skip (send in flight)", flush=True)
                            continue
                        try:
                            send_task.result()
                        except Exception as e:
                            print(f"[{ts()}] Send error: {e}", file=sys.stderr)
                            if not client.is_streaming:
                                break
                        send_task = None

                    # drain audio
                    if audio_queue is not None:
                        while True:
                            try:
                                chunk = audio_queue.get_nowait()
                                audio_buffer = np.concatenate([audio_buffer, chunk])
                            except queue.Empty:
                                break

                    if len(audio_buffer) >= AUDIO_CHUNK_SAMPLES:
                        audio_np = audio_buffer[:AUDIO_CHUNK_SAMPLES]
                        audio_buffer = audio_buffer[AUDIO_CHUNK_SAMPLES:]
                    else:
                        audio_np = audio_buffer
                        audio_buffer = np.array([], dtype=np.float32)
                    audio_b64 = MiniCPMOClient.encode_audio_pcm(audio_np)

                    # get latest frame
                    latest = None
                    while True:
                        try:
                            latest = capture_queue.get_nowait()
                        except queue.Empty:
                            break

                    fc = latest["frame_count"] if latest else 0

                    if latest is not None:
                        frame_index += 1
                        video_frames = [MiniCPMOClient.encode_video_frame(latest["annotated"])]
                    else:
                        video_frames = []

                    if args.debug:
                        audio_len = len(audio_b64)
                        video_len = len(video_frames[0]) if video_frames else 0
                        print(f"\n[{ts()}] >>> frame #{frame_index} (fc={fc} "
                              f"audio={audio_len} video={video_len})", flush=True)

                    send_task = asyncio.create_task(client.send_input_append(audio_b64, video_frames))

            # ── dispatch received ──
            async def dispatch_received():
                loop = asyncio.get_running_loop()
                text_buf: list[str] = []

                def flush_text(text: str) -> None:
                    text = text.strip()
                    if not text:
                        return
                    ev = _match_event(text)
                    if ev:
                        _emit_event(loop, text)
                    else:
                        print(f"\n[{ts()}] [TEXT] {text}", flush=True)

                try:
                    t_get = asyncio.create_task(client.output_queue.get())
                    t_closed = asyncio.create_task(client.closed_event.wait())

                    while True:
                        done, _ = await asyncio.wait(
                            {t_get, t_closed},
                            return_when=asyncio.FIRST_COMPLETED,
                        )

                        if t_closed in done:
                            t_get.cancel()
                            break

                        if t_get not in done:
                            continue

                        kind, data = t_get.result()
                        t_get = asyncio.create_task(client.output_queue.get())

                        if kind == "listen":
                            print(f"\n[{ts()}] [LISTEN] CPM turn ended")
                            flush_text("".join(text_buf))
                            text_buf = []
                        elif kind == "text":
                            text_buf.append(data)
                            text = "".join(text_buf)
                            if _match_event(text):
                                flush_text(text)
                                text_buf = []
                        elif kind == "audio":
                            if args.debug:
                                print(f"[{ts()}] [AUDIO] {len(data)} bytes")

                except asyncio.CancelledError:
                    raise

            # ── session watchdog ──
            async def session_watchdog():
                try:
                    await asyncio.sleep(MINICPMO_SESSION_DURATION)
                    print(f"[{ts()}] Session duration reached ({MINICPMO_SESSION_DURATION}s), reconnecting...")
                except asyncio.CancelledError:
                    pass
                await client.close()

            await asyncio.gather(
                send_loop(),
                dispatch_received(),
                session_watchdog(),
            )

        except (KeyboardInterrupt, asyncio.CancelledError):
            print(f"\n[{ts()}] Stopped.")
            break
        except Exception as e:
            print(f"[{ts()}] Session error: {e}", file=sys.stderr)
            print(f"[{ts()}] Reconnecting in {retry_delay}s...")
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, max_retry_delay)
            continue
        finally:
            await client.close()

        if _stop_capture.is_set():
            break
        print(f"[{ts()}] Session ended, reconnecting...")
        await asyncio.sleep(0.5)

    # ── cleanup ──
    _stop_capture.set()
    cap.release()
    if audio_capture is not None:
        audio_capture.stop()


def main():
    from config import (
        CAMERA_SOURCE, CAMERA_FPS, CAMERA_TOPIC_HEAD, FOXGLOVE_BRIDGE_URL,
        MINICPMO_HOST, MIC_DEVICE_NAME,
    )

    parser = argparse.ArgumentParser(description="MiniCPM-o 事件检测")
    parser.add_argument("--debug", action="store_true", default=_env_bool("DEBUG"))
    parser.add_argument("--source", choices=("local", "foxglove", "zenoh"), default=CAMERA_SOURCE)
    parser.add_argument("--camera", type=int, default=int(_env("CAMERA_ID", "0")))
    parser.add_argument("--width", type=int, default=int(_env("CAMERA_WIDTH", "0")))
    parser.add_argument("--height", type=int, default=int(_env("CAMERA_HEIGHT", "0")))
    parser.add_argument("--fps", type=float, default=CAMERA_FPS)
    parser.add_argument("--foxglove-topic", default=CAMERA_TOPIC_HEAD)
    parser.add_argument("--foxglove-url", default=FOXGLOVE_BRIDGE_URL)
    parser.add_argument("--zenoh-topic", default=_env("ZENOH_TOPIC", "camera/annotated"))
    parser.add_argument("--zenoh-url", default=_env("ZENOH_URL", ""))
    parser.add_argument("--zenoh-fps", type=float, default=_env_float("ZENOH_CLIENT_FPS", 2.0))
    parser.add_argument("--minicpmo-host", default=MINICPMO_HOST)
    parser.add_argument("--insecure", action="store_true", default=_env_bool("INSECURE"))
    parser.add_argument("--no-audio-capture", action="store_true", default=_env_bool("NO_AUDIO_CAPTURE"))
    parser.add_argument("--mic-name", default=MIC_DEVICE_NAME or "")

    args = parser.parse_args()
    print(f"[{ts()}] MiniCPM-o 事件检测启动")

    from config import LOG_LEVEL, LOG_FILE, validate_config
    from logging_config import setup
    validate_config()
    setup(debug=args.debug or LOG_LEVEL.upper() == "DEBUG", log_file=LOG_FILE if LOG_FILE else None)

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()