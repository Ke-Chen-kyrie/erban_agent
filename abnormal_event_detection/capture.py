#!/usr/bin/env python3
"""
CLI video capture -> webinfer adapter -> console output.

Captures frames from a local camera (OpenCV) or a Foxglove/ROS bridge,
sends them to the webinfer adapter's OpenAI-compatible /v1/chat/completions
endpoint. The adapter handles all chunk management, mid-term summaries, and
long-term memory compression. Model responses are printed to stdout.

Usage:
  python capture.py                           # default: camera 0, adapter 127.0.0.1:8070
  python capture.py --source foxglove         # use Foxglove/ROS bridge
  python capture.py --source foxglove --foxglove-topic /camera/image/compressed
  python capture.py --camera 1                # use camera device 1
  python capture.py --adapter http://127.0.0.1:8070/v1
  python capture.py --interval 2.0            # capture every 2 seconds
  python capture.py --prompt "Detect abnormal events"
"""

import argparse
import base64
import io
import os
import sys
import time
import uuid
from datetime import datetime

import cv2
from openai import OpenAI
from PIL import Image


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def encode_frame(frame, bgr_input: bool = True) -> str:
    """Convert frame to base64 JPEG string.

    Args:
        frame: numpy array (BGR if bgr_input=True, RGB otherwise).
        bgr_input: True for OpenCV camera (BGR), False for Foxglove (RGB).
    """
    if bgr_input:
        frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
    img = Image.fromarray(frame)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _create_camera_source(args):
    """Create an OpenCV camera source."""
    cap = cv2.VideoCapture(args.camera)
    if not cap.isOpened():
        print(f"Error: cannot open camera {args.camera}", file=sys.stderr)
        sys.exit(1)
    if args.width > 0:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
    if args.height > 0:
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
    return cap, True  # (capture, bgr_input)


def _create_foxglove_source(args):
    """Create a Foxglove/ROS image source."""
    from foxglove_client import FoxgloveImageCapture

    cap = FoxgloveImageCapture(topic=args.foxglove_topic, url=args.foxglove_url or None)
    return cap, False  # (capture, bgr_input) — Foxglove returns RGB


def main():
    parser = argparse.ArgumentParser(description="CLI video capture -> VLM -> console")
    parser.add_argument(
        "--source", choices=("camera", "foxglove"), default=os.getenv("VIDEO_SOURCE", "camera"),
        help="Video source: camera (OpenCV) or foxglove (ROS bridge). Also via VIDEO_SOURCE env.",
    )
    parser.add_argument("--camera", type=int, default=int(os.getenv("CAMERA_ID", "0")), help="Camera device ID")
    parser.add_argument(
        "--foxglove-topic", default=os.getenv("FOXGLOVE_TOPIC", "/camera/image/compressed"),
        help="Foxglove/ROS image topic",
    )
    parser.add_argument(
        "--foxglove-url", default=os.getenv("FOXGLOVE_BRIDGE_URL", ""),
        help="Foxglove bridge WebSocket URL (default from FOXGLOVE_BRIDGE_URL env)",
    )
    parser.add_argument("--adapter", default=os.getenv("ADAPTER_API_BASE") or f"http://{os.getenv('ADAPTER_HOST', '127.0.0.1')}:{os.getenv('ADAPTER_PORT', '8070')}/v1", help="Adapter API base URL")
    parser.add_argument("--interval", type=float, default=float(os.getenv("CAPTURE_INTERVAL", "1.0")), help="Capture interval in seconds")
    parser.add_argument("--prompt", default="", help="Optional text prompt appended to each frame")
    parser.add_argument("--model", default="streaming-infer-adapter", help="Model name sent to adapter")
    parser.add_argument("--show-silence", action="store_true", help="Print a dot for each silence")
    parser.add_argument("--width", type=int, default=int(os.getenv("CAMERA_WIDTH", "0")), help="Capture width (0 = native)")
    parser.add_argument("--height", type=int, default=int(os.getenv("CAMERA_HEIGHT", "0")), help="Capture height (0 = native)")
    args = parser.parse_args()

    session_id = uuid.uuid4().hex[:12]
    client = OpenAI(base_url=args.adapter, api_key="EMPTY")

    if args.source == "foxglove":
        cap, bgr_input = _create_foxglove_source(args)
        source_desc = f"Foxglove topic={args.foxglove_topic}"
    else:
        cap, bgr_input = _create_camera_source(args)
        source_desc = f"Camera {args.camera} ({args.width}x{args.height})"

    print(f"[{ts()}] Session: {session_id}")
    print(f"[{ts()}] Source: {source_desc}")
    print(f"[{ts()}] Adapter: {args.adapter}")
    print(f"[{ts()}] Interval: {args.interval}s")
    print(f"[{ts()}] Waiting for first response...")
    print("-" * 50)

    frame_count = 0
    last_capture = 0.0

    try:
        while True:
            if args.source == "camera":
                ret, frame = cap.read()
            else:
                ret, frame = cap.read(timeout=1.0)

            if not ret or frame is None:
                print(f"[{ts()}] Frame read failed, retrying...", file=sys.stderr)
                time.sleep(0.5)
                continue

            now = time.time()
            if now - last_capture < args.interval:
                time.sleep(0.01)
                continue

            last_capture = now
            frame_count += 1

            img_b64 = encode_frame(frame, bgr_input=bgr_input)
            image_url = f"data:image/jpeg;base64,{img_b64}"

            content = []
            if args.prompt:
                content.append({"type": "text", "text": args.prompt})
            content.append({"type": "image_url", "image_url": {"url": image_url}})

            try:
                response = client.chat.completions.create(
                    model=args.model,
                    messages=[{"role": "user", "content": content}],
                    max_tokens=256,
                    temperature=0.7,
                    extra_headers={"x-streaming-session": session_id},
                )
                text = response.choices[0].message.content or ""
            except Exception as e:
                print(f"[{ts()}] API error: {e}", file=sys.stderr)
                continue

            text = text.strip()

            if text.startswith("</silence>"):
                if args.show_silence:
                    print(".", end="", flush=True)
            elif text.startswith("</response>"):
                body = text.removeprefix("</response>").strip()
                print(f"\n[{ts()}] ** {body}")
            else:
                print(f"\n[{ts()}] {text}")

    except KeyboardInterrupt:
        print(f"\n[{ts()}] Stopped. {frame_count} frames captured.")
    finally:
        cap.release()


if __name__ == "__main__":
    main()