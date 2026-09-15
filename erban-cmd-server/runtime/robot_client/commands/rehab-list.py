"""列出当前方向下所有可用轨迹类型 — 发送 {"command": "rehab-list"}."""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="列出当前方向下所有可用轨迹类型")


def main() -> int:
    build_parser().parse_args()
    run_robot_command({"command": "rehab-list"}, "列出轨迹")
    return 0


if __name__ == "__main__":
    sys.exit(main())