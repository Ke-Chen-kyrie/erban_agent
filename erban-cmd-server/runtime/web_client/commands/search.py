"""联网搜索, 返回摘要 (iQS API). 多词查询用空格拼合, 如: search 今天杭州天气"""

import argparse
import sys

from web_client import search_web


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="联网搜索返回结果摘要")
    parser.add_argument("query", nargs="+", help="搜索词 (多词用空格拼合)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    print(search_web(" ".join(args.query)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
