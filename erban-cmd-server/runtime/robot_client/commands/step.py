"""底盘小幅位移/转向 — 发送 {"command": "step"}.

translate 或 rotate 至少给其一 (都不给则机器人侧忽略).
"""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="底盘小幅位移/转向")
    parser.add_argument("--translate", help="front/back/left/right 或 {x:米, y:米}")
    parser.add_argument("--rotate", help="left/right 或 度数")
    parser.add_argument(
        "-e",
        "--extent",
        choices=["slight", "moderate", "significant"],
        default="moderate",
        help="移动距离/转角档位",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload = {"command": "step", "extent": args.extent}
    if args.translate:
        payload["translate"] = args.translate
    if args.rotate:
        payload["rotate"] = args.rotate
    run_robot_command(payload, "底盘位移")
    return 0


if __name__ == "__main__":
    sys.exit(main())