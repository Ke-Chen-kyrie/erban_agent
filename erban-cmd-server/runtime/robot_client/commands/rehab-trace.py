"""执行康复运动轨迹 — 发送 {"command": "rehab-trace"}.

name 为有效轨迹名; side/confirm 可选, 不写则不发送该字段
(扶肩/抓腿等动作需显式 --confirm true).
"""

import argparse
import sys

from robot_client import run_robot_command

TRACES = [
    "elbow_flexion",
    "forearm_rotation",
    "shoulder_flexion",
    "leg_raise",
]


def _bool_value(s: str) -> str:
    s = s.strip().lower()
    if s not in ("true", "false"):
        raise argparse.ArgumentTypeError("取值须为 true/false")
    return s


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="执行康复运动轨迹")
    parser.add_argument(
        "-n",
        "--name",
        choices=TRACES,
        required=True,
        help=(
            "轨迹名 elbow_flexion=肘部上下, forearm_rotation=腕部旋转, "
            "shoulder_flexion=扶肩, leg_raise=抓腿上抬"
        ),
    )
    parser.add_argument(
        "-d",
        "--side",
        choices=["left", "right"],
        help="在哪侧做 (不写则由机器人决定)",
    )
    parser.add_argument(
        "--confirm",
        type=_bool_value,
        metavar="true|false",
        default="true",
        help=(
            "true=真正执行动作 (默认), false=只计算目标点不发动作"
        ),
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    payload: dict = {"command": "rehab-trace", "name": args.name, "confirm": args.confirm}
    if args.side:
        payload["side"] = args.side
    run_robot_command(payload, f"执行轨迹 {args.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())