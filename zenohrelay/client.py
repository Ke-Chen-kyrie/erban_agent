#!/usr/bin/env python3
"""ZenohImageCapture 命令行示例。"""

from __future__ import annotations

import argparse
import signal
import threading
import time
from pathlib import Path

import cv2

from config import env
from logging_config import configure_logging
from zenoh_client import ZenohImageCapture


def main() -> int:
    parser = argparse.ArgumentParser(description="订阅 Zenoh 标注图像")
    parser.add_argument("--topic", default=env("ZENOH_TOPIC", "camera/annotated"))
    parser.add_argument("--url", default=env("ZENOH_URL", "tcp/127.0.0.1:7447"))
    parser.add_argument("--fps", type=float, default=0.0, help="本客户端最大取用帧率；0=每个新帧")
    parser.add_argument("--show", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--save", help="把最近帧写入指定 JPEG 文件")
    parser.add_argument("--print-meta", action="store_true")
    args = parser.parse_args()
    configure_logging(str(env("LOG_LEVEL", "INFO")))

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    capture = ZenohImageCapture(args.topic, args.url or None, fps=args.fps)
    last_sequence = 0
    interval = 1.0 / args.fps if args.fps > 0 else 0.0

    try:
        while not stop.is_set():
            success, rgb = capture.read(timeout=max(1.0, interval + 0.5))
            if not success or rgb is None:
                continue
            sequence = capture.frame_sequence
            if sequence == last_sequence:
                time.sleep(0.01)
                continue
            last_sequence = sequence
            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)

            if args.print_meta and capture.latest_meta is not None:
                print(capture.latest_meta, flush=True)
            if args.save:
                output = Path(args.save)
                output.parent.mkdir(parents=True, exist_ok=True)
                cv2.imwrite(str(output), bgr)
            if args.show:
                cv2.imshow("Zenoh annotated", bgr)
                if cv2.waitKey(1) & 0xFF in (27, ord("q")):
                    break
            if args.once:
                break
    finally:
        capture.release()
        if args.show:
            cv2.destroyAllWindows()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
