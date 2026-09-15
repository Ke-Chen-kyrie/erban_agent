"""检查指定用户是否在摄像头画面中 — 抓帧 + 人脸验证.

匹配退 0, 不匹配/失败 退 1. 端点由 ident_client 从 config 自读.
"""

import argparse
from pathlib import Path
import sys

from ident_client import camera_topic
from ident_client import camera_ws_url
from ident_client import verify_face
from ident_client.camera import capture_ros_frame


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检查指定用户是否在摄像头画面中")
    parser.add_argument("-u", "--user-id", required=True, help="用户ID")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    frame_path = None
    try:
        frame_path = capture_ros_frame(
            camera_ws_url(),
            camera_topic(),
        )
        if not frame_path:
            print("无法从摄像头获取画面, 请检查摄像头连接", file=sys.stderr)
            return 1
        result = verify_face(args.user_id, frame_path)
        matched = bool(result.get("match", False))
        score = result.get("score", 0)
        if matched:
            print(f"用户 {args.user_id} 在摄像头画面中（置信度: {score:.2f}）")
        else:
            print(f"用户 {args.user_id} 不在摄像头画面中（置信度: {score:.2f}）", file=sys.stderr)
        return 0 if matched else 1
    except Exception as e:
        print(f"人脸验证失败: {e}", file=sys.stderr)
        return 1
    finally:
        if frame_path:
            Path(frame_path).unlink(missing_ok=True)  # 用完删临时帧


if __name__ == "__main__":
    sys.exit(main())
