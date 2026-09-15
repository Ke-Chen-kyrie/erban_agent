"""回到双手向下、目视前方的姿态 — 发送 {"command": "reset"}."""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="回到双手向下、目视前方的姿态")


def main() -> int:
    build_parser().parse_args()
    run_robot_command({"command": "reset"}, "回到双手向下、目视前方的姿态")
    return 0


if __name__ == "__main__":
    sys.exit(main())