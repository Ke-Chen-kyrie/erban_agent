"""伸出到康复运动起点 — 发送 {"command": "rehab-extend"}."""

import argparse
import sys

from robot_client import run_robot_command


def _bool_value(s: str) -> str:
    s = s.strip().lower()
    if s not in ("true", "false"):
        raise argparse.ArgumentTypeError("取值须为 true/false")
    return s


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="伸出到康复运动起点")
    parser.add_argument(
        "--wait-contact",
        type=_bool_value,
        metavar="true|false",
        default="true",
        help="等到接触到位后才返回 (true=接触确认成功后才完成, false=不等接触直接伸出)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload: dict = {"command": "rehab-extend", "wait_contact": args.wait_contact}
    run_robot_command(payload, "伸出康复臂")
    return 0


if __name__ == "__main__":
    sys.exit(main())