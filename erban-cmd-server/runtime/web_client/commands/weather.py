"""查询实时天气与 7 天预报 (iQS API). 多词城市用空格拼合, 如: weather 浙江 杭州"""

import argparse
import sys

from web_client import get_weather


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="查询城市实时天气与预报")
    parser.add_argument("city", nargs="+", help="城市名 (多词用空格拼合)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    print(get_weather(" ".join(args.city)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
