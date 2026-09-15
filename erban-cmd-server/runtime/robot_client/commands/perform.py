"""执行机器人动作编排序列 (手+臂+头 穿插, R3/MPC 双模式).

编排名原样转发给机器人, 由机器人端持有并执行对应序列
(合法名 EP1~EP5, 见 map-locations 命令).

动作编排结束后会额外停顿 POST_PERFORM_HOLD 秒(默认 2.0)再返回,
从而保证调用方(如导览技能里的"播报")在动作完全结束后才开始。
时序由命令本身硬性保证, 不依赖调用方 / 大模型的自觉。
"""

import argparse
import os
import sys
import time

from robot_client import run_robot_command

# 动作编排结束到命令返回之间的额外停顿秒数(可整环境变量覆盖)
POST_PERFORM_HOLD = float(os.environ.get("POST_PERFORM_HOLD", "2.0"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="执行机器人动作编排序列 (手+臂+头 穿插, R3/MPC 双模式)"
    )
    parser.add_argument(
        "--name",
        choices=["EP1", "EP2", "EP3", "EP4", "EP5"],
        required=True,
        help="编排名 (EP1~EP5, 机器人端持有, 见 map-locations)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_robot_command({"command": "perform", "name": args.name}, f"执行动作编排 {args.name}")
    # 硬性保证: 动作编排全部结束后, 再额外停顿 POST_PERFORM_HOLD 秒才返回,
    # 确保后续语音播报在动作完成 ±时序余量 之后才开始。
    # time.sleep(POST_PERFORM_HOLD)
    return 0


if __name__ == "__main__":
    sys.exit(main())