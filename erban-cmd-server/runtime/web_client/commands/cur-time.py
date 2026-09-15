"""联网获取当前时间 (worldtimeapi), 失败回退本地 Asia/Shanghai."""

import argparse
import sys

from web_client import get_current_time


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(description="获取当前时间")


def main() -> int:
    build_parser().parse_args()
    print(get_current_time())
    return 0


if __name__ == "__main__":
    sys.exit(main())
