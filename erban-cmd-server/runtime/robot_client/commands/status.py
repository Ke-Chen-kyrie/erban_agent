"""查询机器人状态 — 发送 {"command": "status"}."""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="查询机器人状态")


def main() -> int:
    build_parser().parse_args()
    run_robot_command({"command": "status"}, "查询状态")
    return 0


if __name__ == "__main__":
    sys.exit(main())