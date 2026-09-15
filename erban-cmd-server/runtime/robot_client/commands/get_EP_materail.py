"""获取参观讲解词 — 从 Langfuse 拉取指定 EP (EP1~EP5) 的播报词并打印."""

import argparse
import sys

from robot_client import fetch_ep_material


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="获取指定 EP 的参观讲解播报词")
    parser.add_argument(
        "ep",
        choices=["EP1", "EP2", "EP3", "EP4", "EP5"],
        help="讲解片段编号, 如 EP1/EP2/EP3/EP4/EP5",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        print(fetch_ep_material(args.ep))
    except Exception as e:
        print(f"获取讲解词失败: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())