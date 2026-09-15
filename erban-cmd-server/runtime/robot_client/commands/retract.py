"""收回递出的杯/勺 — 发送 {"command": "retract"}."""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="收回递出的杯/勺")
    parser.add_argument(
        "-o",
        "--object",
        choices=["cup", "spoon"],
        required=True,
        help="cup=水杯, spoon=勺子",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_robot_command({"command": "retract", "object": args.object}, f"收回{args.object}")
    return 0


if __name__ == "__main__":
    sys.exit(main())