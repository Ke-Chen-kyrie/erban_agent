"""做出手势 — 发送 {"command": "gesture", "name": <手势名>}."""

import argparse
import sys

from robot_client import run_robot_command

GESTURES = [
    "like",
    "wave_hand",
    "salute",
    "clap",
    "quiet",
    "cheer_on",
    "guide_left",
    "guide_right",
    "show_heart",
    "fist_salute",
]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="做出手势 (点赞/挥手/敬礼等)")
    parser.add_argument(
        "--name",
        choices=GESTURES,
        default="like",
        help=(
            "手势名称 like=点赞, wave_hand=挥手, salute=敬礼, clap=鼓掌, quiet=嘘 (安静), "
            "cheer_on=加油, guide_left=左指引, guide_right=右指引, show_heart=比心, "
            "fist_salute=抱拳"
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_robot_command({"command": "gesture", "name": args.name}, f"做出手势 {args.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
