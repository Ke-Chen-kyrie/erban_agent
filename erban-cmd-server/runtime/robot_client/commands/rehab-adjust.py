"""上/下微调选中臂高度 — 发送 {"command": "rehab-adjust"}."""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="上/下微调选中臂高度")
    parser.add_argument(
        "-d",
        "--direction",
        choices=["up", "down"],
        required=True,
        help="up=上调, down=下调",
    )
    parser.add_argument(
        "-e",
        "--extent",
        choices=["slight", "moderate", "significant"],
        default="moderate",
        help="微调幅度档位",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_robot_command(
        {"command": "rehab-adjust", "direction": args.direction, "extent": args.extent},
        f"微调{args.direction}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())