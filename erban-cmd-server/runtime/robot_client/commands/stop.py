"""打断当前动作 — 发送 {"command": "stop"}."""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="打断当前动作")


def main() -> int:
    build_parser().parse_args()
    run_robot_command({"command": "stop"}, "打断")
    return 0


if __name__ == "__main__":
    sys.exit(main())