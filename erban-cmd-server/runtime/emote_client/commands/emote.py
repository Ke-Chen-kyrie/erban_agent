"""循环播放指定机器表情 via rosbridge media_play 服务."""

import argparse
import sys

from emote_client import EMOTIONS, play_emotion


def build_parser() -> argparse.ArgumentParser:
    choices_semantics = ", ".join(f"{k}={v['name']}" for k, v in EMOTIONS.items())
    parser = argparse.ArgumentParser(description="切换机器人表情")
    parser.add_argument(
        "emotion",
        choices=list(EMOTIONS),
        help=f"表情, 取值: {choices_semantics}",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if play_emotion(args.emotion):
        print(f"播放表情 {EMOTIONS[args.emotion]['name']} 成功 (循环中)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())