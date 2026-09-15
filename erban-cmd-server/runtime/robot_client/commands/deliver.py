"""递杯喂水 / 舀饭喂食 — 发送 {"command": "deliver"}.

object cup=递杯喂水, spoon=递勺喂饭; user_id 必填目标用户.
"""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="递杯喂水 / 递勺喂饭")
    parser.add_argument(
        "-o",
        "--object",
        choices=["cup", "spoon"],
        required=True,
        help="cup=递杯喂水, spoon=递勺喂饭",
    )
    parser.add_argument("-u", "--user-id", required=True, help="目标用户ID")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_robot_command(
        {"command": "deliver", "object": args.object, "user_id": args.user_id},
        f"递送{args.object}",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())