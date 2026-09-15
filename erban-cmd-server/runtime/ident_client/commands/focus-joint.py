"""聚焦关注用户指定身体部位 — POST 关节服务 {"部位": 1}.

传 ["整体"] 可聚焦全部 24 个 SKEL 关节. 部位支持中英文:
骨盆(pelvis) 右髋(femur_r) 右膝(tibia_r) 右踝(talus_r) 右跟骨(calcn_r) 右趾(toes_r)
右肩胛(scapula_r) 右肩(humerus_r) 右肘(ulna_r) 右腕(radius_r) 右手(hand_r)
左髋(femur_l) 左膝(tibia_l) 左踝(talus_l) 左跟骨(calcn_l) 左趾(toes_l)
左肩胛(scapula_l) 左肩(humerus_l) 左肘(ulna_l) 左腕(radius_l) 左手(hand_l)
腰椎(lumbar_body) 胸椎(thorax) 头(head)
"""

import argparse
import sys

from ident_client import focus_joints


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="聚焦关注用户的特定身体部位, 增强该部位感知与分析能力")
    parser.add_argument(
        "-p",
        "--parts",
        action="append",
        default=[],
        help="要聚焦的身体部位 (中英文均可, 可重复指定; 传 整体 聚焦全部 24 关节)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if not args.parts:
        args.parts = ["整体"]
    code, report = focus_joints(args.parts)
    print(report)
    return code


if __name__ == "__main__":
    sys.exit(main())