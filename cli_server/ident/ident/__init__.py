import os
import sys
import argparse

from robot_common import env
from robot_common.camera import capture_ros_frame
from IdentificationClient import IdentificationClient


def main():
    parser = argparse.ArgumentParser(description="检查指定用户是否在摄像头画面中")
    parser.add_argument("-u", "--user-id", required=True, help="用户 ID")
    args = parser.parse_args()

    conf = env()
    frame_path = capture_ros_frame(conf["camera_ws_url"], conf["camera_topic"])
    if not frame_path:
        print("无法从摄像头获取画面，请检查摄像头连接", file=sys.stderr)
        sys.exit(1)

    try:
        client = IdentificationClient(conf["identification_url"])
        result = client.face_verify(args.user_id, frame_path)

        matched = result.get("match", False)
        score = result.get("score", 0)
        if matched:
            print(f"用户 {args.user_id} 在摄像头画面中（置信度: {score:.2f}）")
        else:
            print(f"用户 {args.user_id} 不在摄像头画面中（置信度: {score:.2f}）")
        sys.exit(0 if matched else 1)
    except Exception as e:
        print(f"人脸验证失败: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        try:
            os.unlink(frame_path)
        except OSError:
            pass