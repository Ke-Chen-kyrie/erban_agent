"""舀一勺食物 — 发送 {"command": "scoop"}.

无参数.
"""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="舀一勺食物")


def main() -> int:
    build_parser().parse_args()
    run_robot_command({"command": "scoop"}, "舀食物")
    return 0


if __name__ == "__main__":
    sys.exit(main())
