"""地图导航: 地点/坐标直达, 或导航到目标用户前方指定侧.

mode 缺省按参数自动推: user_id→person, location→地点, position→坐标.
location 地点名原样转发给机器人解析 (robot 端持有, 见 map-locations 命令).
"""

import argparse
import sys

from robot_client import navigation_standoff_distance, run_robot_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="地图导航: 地点/坐标直达, 或导航到目标用户前方指定侧"
    )
    parser.add_argument(
        "--mode",
        choices=["person", "location"],
        help="导航模式, 缺省按参数自动推 (user_id→person, location→地点, position→坐标)",
    )
    parser.add_argument(
        "-u", "--user-id", help="目标用户ID (mode=person, 导航到人前方)"
    )
    parser.add_argument(
        "-n", "--location", help="地点名 (mode=location, 原样转机器人解析; 见 map-locations)"
    )
    parser.add_argument(
        "-p", "--position", help="坐标字符串 'x,y[,yaw_deg]', 相对 --frame"
    )
    parser.add_argument(
        "-d",
        "--direction",
        choices=["front", "back", "left", "right"],
        default="front",
        help="mode=person 停在目标用户哪侧",
    )
    parser.add_argument(
        "-f",
        "--frame",
        choices=["base", "map"],
        default="base",
        help="position 坐标系 (base=机器人系, map=地图系), 内部转 map",
    )
    parser.add_argument(
        "--standoff",
        type=float,
        help="mode=person 站位距离(米), 缺省取配置 standoff_distance",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    mode = args.mode
    if mode is None:
        if args.user_id:
            mode = "person"
        elif args.location:
            mode = "location"
        elif args.position:
            mode = "position"
        else:
            print("错误: 至少给一个 user_id / location / position", file=sys.stderr)
            return 1

    payload = {"command": "navigate"}
    if mode == "person":
        if not args.user_id:
            print("错误: person 模式需 --user-id", file=sys.stderr)
            return 1
        payload["user_id"] = args.user_id
        payload["direction"] = args.direction
        standoff = args.standoff if args.standoff is not None else navigation_standoff_distance()
        payload["standoff"] = standoff
        label = f"导航到用户 {args.user_id} ({args.direction})"
    elif mode == "location":
        if not args.location:
            print("错误: location 模式需 --location", file=sys.stderr)
            return 1
        payload["location"] = args.location
        payload["mode"] = "location"
        label = f"导航到地点 {args.location}"
    else:  # position
        if not args.position:
            print("错误: position 模式需 --position", file=sys.stderr)
            return 1
        payload["position"] = args.position
        payload["frame"] = args.frame
        label = "导航到坐标"

    run_robot_command(payload, label)
    return 0


if __name__ == "__main__":
    sys.exit(main())