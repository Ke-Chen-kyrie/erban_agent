"""退出康复模式并收回 — 发送 {"command": "rehab-retract"}."""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="退出康复模式并收回")


def main() -> int:
    build_parser().parse_args()
    run_robot_command({"command": "rehab-retract"}, "退出康复")
    return 0


if __name__ == "__main__":
    sys.exit(main())