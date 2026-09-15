"""转腰面向与/或头部俯仰 — 发送 {"command": "orient"}.

各参数可独立省略, 省略保持当前.
"""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="面向 front|table(桌子) / left|user(用户)")
    parser.add_argument(
        "-f",
        "--facing",
        choices=["front", "table", "left", "user"],
        help="front/table=面向桌子, left/user=面向用户",
    )
    parser.add_argument(
        "-l",
        "--looking",
        choices=["top", "up", "mid", "down", "bottom"],
        help="头部俯仰档位",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = {"command": "orient"}
    if args.facing:
        payload["facing"] = args.facing
    if args.looking:
        payload["looking"] = args.looking
    run_robot_command(payload, "转腰/抬头")
    return 0


if __name__ == "__main__":
    sys.exit(main())