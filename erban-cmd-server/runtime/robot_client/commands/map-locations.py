"""列出机器人已配置的导航地点 — 发送 {"command": "map-locations"}."""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="列出已配置的导航地点")


def main() -> int:
    build_parser().parse_args()
    run_robot_command({"command": "map-locations"}, "查询地图地点")
    return 0


if __name__ == "__main__":
    sys.exit(main())