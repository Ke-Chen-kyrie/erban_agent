"""物体或人脸检测 — 发送 {"command": "detect"}."""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="物体或人脸检测")
    parser.add_argument(
        "-o",
        "--object",
        choices=["cup", "bowl", "spoon"],
        help="物体检测目标",
    )
    parser.add_argument("-u", "--user", help="目标用户ID (人脸检测)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = {"command": "detect"}
    if args.object:
        payload["object"] = args.object
    if args.user:
        payload["user"] = args.user
    run_robot_command(payload, "检测")
    return 0


if __name__ == "__main__":
    sys.exit(main())