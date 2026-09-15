#!/usr/bin/env python3
"""Foxglove → 人脸识别标注 → Zenoh 发布服务。"""

from __future__ import annotations

import argparse
import signal
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone

import cv2

from config import Settings, make_zenoh_config
from camera_client import LocalCameraCapture
from face_detect import FaceAnnotator
from foxglove_client import FoxgloveImageCapture, RosbridgeImageCapture
from logging_config import configure_logging, get_logger
from protocol import encode_frame


logger = get_logger("zenoh-relay-server")


def _faces_metadata(faces: list[dict]) -> list[dict]:
    fields = ("user_id", "name", "score", "matched", "location")
    return [{key: face.get(key) for key in fields} for face in faces]


def run(settings: Settings, stop: threading.Event | None = None) -> int:
    stop = stop or threading.Event()
    if settings.video_source == "camera":
        capture = LocalCameraCapture(
            settings.camera_id,
            settings.camera_width,
            settings.camera_height,
        )
        source_description = f"本地摄像头 /dev/video{settings.camera_id}"
    elif settings.video_source == "rosbridge":
        capture = RosbridgeImageCapture(
            settings.foxglove_topic, settings.rosbridge_url
        )
        source_description = (
            f"ROS1 rosbridge {settings.foxglove_topic} @ {settings.rosbridge_url}"
        )
    else:
        capture = FoxgloveImageCapture(settings.foxglove_topic, settings.foxglove_url)
        source_description = f"Foxglove {settings.foxglove_topic} @ {settings.foxglove_url}"
    max_fps = settings.max_fps
    annotator = FaceAnnotator(
        settings.ident_url,
        timeout=settings.ident_timeout,
        threshold=settings.ident_threshold,
        max_face_num=settings.max_face_num,
    )

    import zenoh

    session = zenoh.open(make_zenoh_config(settings.zenoh_url, settings.zenoh_listen_url))
    source_sequence = 0
    published_sequence = 0
    last_publish = 0.0
    last_error_log = 0.0
    min_interval = 1.0 / max_fps if max_fps > 0 else 0.0

    logger.info("图像源: %s", source_description)
    logger.info("Face API: %s/api/face/detect", settings.ident_url)
    logger.info(
        "Zenoh: topic=%s listen=%s connect=%s payload=%s",
        settings.zenoh_topic,
        settings.zenoh_listen_url,
        settings.zenoh_url or "(无显式连接)",
        "metadata+JPEG" if settings.include_metadata else "JPEG",
    )
    logger.info(
        "采集节拍: %s",
        f"最高 {max_fps:g} FPS" if max_fps else "自适应识别耗时",
    )

    try:
        while not stop.is_set():
            if min_interval:
                remaining = min_interval - (time.monotonic() - last_publish)
                if remaining > 0 and stop.wait(remaining):
                    break

            success, rgb, source_sequence, source_timestamp_ns = capture.read_next(
                source_sequence, timeout=1.0
            )
            if not success or rgb is None:
                continue

            bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
            started = time.monotonic()
            faces: list[dict] = []
            annotated = False
            try:
                output_bgr, faces = annotator.annotate(bgr)
                annotated = True
            except Exception as exc:
                output_bgr = bgr
                now = time.monotonic()
                if now - last_error_log >= 10:
                    logger.warning("人脸识别不可用，降级发布原图: %s", exc)
                    last_error_log = now
            processing_ms = round((time.monotonic() - started) * 1000, 1)

            ok, jpeg = cv2.imencode(
                ".jpg",
                output_bgr,
                [cv2.IMWRITE_JPEG_QUALITY, settings.jpeg_quality],
            )
            if not ok:
                logger.warning("标注帧 JPEG 编码失败")
                continue

            metadata = None
            if settings.include_metadata:
                metadata = {
                    "seq": published_sequence,
                    "source_seq": source_sequence,
                    "source_timestamp_ns": source_timestamp_ns,
                    "published_at": datetime.now(timezone.utc).isoformat(),
                    "processing_ms": processing_ms,
                    "annotated": annotated,
                    "faces": _faces_metadata(faces),
                }
            session.put(settings.zenoh_topic, encode_frame(jpeg.tobytes(), metadata))
            published_sequence += 1
            last_publish = time.monotonic()

            if published_sequence == 1 or published_sequence % 30 == 0:
                logger.info(
                    "已发布 %d 帧，当前 faces=%d processing=%.1fms",
                    published_sequence,
                    len(faces),
                    processing_ms,
                )
    finally:
        capture.release()
        annotator.close()
        session.close()
        logger.info("服务已停止，共发布 %d 帧", published_sequence)
    return 0


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="摄像头人脸标注 Zenoh 中继服务")
    parser.add_argument("--source", choices=("foxglove", "camera", "rosbridge"))
    parser.add_argument("--foxglove-url")
    parser.add_argument("--foxglove-topic")
    parser.add_argument("--rosbridge-url")
    parser.add_argument("--camera-id", type=int)
    parser.add_argument("--camera-width", type=int)
    parser.add_argument("--camera-height", type=int)
    parser.add_argument("--topic", help="Zenoh topic/key expression")
    parser.add_argument("--url", help="Zenoh 显式连接 endpoint，如 tcp/192.168.1.10:7447")
    parser.add_argument(
        "--listen-url",
        help="服务端监听的固定端口，如 tcp/0.0.0.0:7447",
    )
    parser.add_argument("--metadata", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--max-fps", type=float)
    parser.add_argument("--jpeg-quality", type=int)
    parser.add_argument("--ident-url")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    settings = Settings.from_env()
    overrides = {
        "video_source": args.source,
        "foxglove_url": args.foxglove_url,
        "foxglove_topic": args.foxglove_topic,
        "rosbridge_url": args.rosbridge_url,
        "camera_id": args.camera_id,
        "camera_width": args.camera_width,
        "camera_height": args.camera_height,
        "zenoh_topic": args.topic,
        "zenoh_url": args.url,
        "zenoh_listen_url": args.listen_url,
        "include_metadata": args.metadata,
        "max_fps": args.max_fps,
        "jpeg_quality": args.jpeg_quality,
        "ident_url": args.ident_url,
    }
    settings = replace(settings, **{key: value for key, value in overrides.items() if value is not None})
    settings.validate()
    configure_logging(settings.log_level)

    stop = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    return run(settings, stop)


if __name__ == "__main__":
    raise SystemExit(main())
