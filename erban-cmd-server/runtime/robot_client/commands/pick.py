"""拾取物品 — 发送 {"command": "pick"}.

object cup=水杯, bowl=碗勺. 拾取后处于高处.
"""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="拾取物品 (cup/bowl)")
    parser.add_argument(
        "-o",
        "--object",
        choices=["cup", "bowl"],
        required=True,
        help="cup=水杯, bowl=碗勺",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_robot_command({"command": "pick", "object": args.object}, f"拾取{args.object}")
    return 0


if __name__ == "__main__":
    sys.exit(main())