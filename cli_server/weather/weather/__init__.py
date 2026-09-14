import sys
import argparse
from robot_common import get_weather


def main():
    parser = argparse.ArgumentParser(description="查询天气")
    parser.add_argument("city", nargs="+", help="城市名称，如 北京、杭州")
    args = parser.parse_args()

    city = " ".join(args.city)
    result = get_weather(city)
    print(result)
    sys.exit(0)