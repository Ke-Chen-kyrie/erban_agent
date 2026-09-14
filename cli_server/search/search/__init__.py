import sys
import argparse
from robot_common import search_web


def main():
    parser = argparse.ArgumentParser(description="联网搜索")
    parser.add_argument("query", nargs="+", help="搜索关键词")
    args = parser.parse_args()

    query = " ".join(args.query)
    result = search_web(query)
    print(result)
    sys.exit(0)