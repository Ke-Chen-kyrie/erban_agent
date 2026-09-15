from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import cv2

from app.config import load_config
from foxglove_client import FoxgloveClient, FoxgloveImageCapture


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Probe Foxglove robot cameras and capture one frame.")
    parser.add_argument("--config", type=Path, default=Path.cwd() / "config.yaml")
    parser.add_argument("--url", help="Foxglove bridge URL, for example ws://192.168.217.100:8768")
    parser.add_argument("--camera", choices=["head", "up"], default="head")
    parser.add_argument("--topic", help="Override camera topic")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--output", type=Path, help="Output jpg path")
    parser.add_argument("--list-topics", action="store_true")
    return parser.parse_args()


async def list_topics(url: str) -> None:
    client = FoxgloveClient(url)
    await client.connect(discovery_timeout=4.0)
    try:
        print(f"connected: {url}")
        print(f"topics: {len(client.topics)}")
        for topic in client.topics:
            print(topic)
    finally:
        await client.close()


def capture_frame(url: str, topic: str, output: Path, timeout: float) -> None:
    print(f"connecting: {url}")
    print(f"topic: {topic}")
    capture = FoxgloveImageCapture(topic, url=url)
    try:
        ok, frame_rgb = capture.read(timeout=timeout)
        if not ok or frame_rgb is None:
            raise RuntimeError(f"{timeout:.0f}s 内没有收到图像帧")
        output.parent.mkdir(parents=True, exist_ok=True)
        frame_bgr = cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
        if not cv2.imwrite(str(output), frame_bgr):
            raise RuntimeError(f"写入失败：{output}")
        height, width = frame_rgb.shape[:2]
        print(f"saved: {output}")
        print(f"size: {width}x{height}")
    finally:
        capture.release()


def main() -> int:
    args = parse_args()
    config = load_config(args.config)
    url = args.url or config.foxglove_bridge_url
    topic = args.topic or (config.camera_topic_head if args.camera == "head" else config.camera_topic_up)

    if args.list_topics:
        asyncio.run(list_topics(url))
        return 0

    output = args.output or ROOT / "tmp" / f"robot_{args.camera}_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
    capture_frame(url, topic, output, args.timeout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
