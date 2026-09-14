import sys
import argparse
from robot_common.client import RobotClient
from robot_common import env


def _run(command, label):
    conf = env()
    client = RobotClient(conf["host"], conf["port"], conf["timeout"])
    if not client.connect():
        print("连接机器人失败", file=sys.stderr)
        sys.exit(1)
    result = client.send_command(command)
    client.disconnect()
    if result and result.get("success", True):
        print(result.get("message", f"{label}成功"))
    else:
        msg = result.get("message", "失败") if result else "无响应"
        print(f"{label}失败: {msg}", file=sys.stderr)
        sys.exit(1)


def turn_main():
    parser = argparse.ArgumentParser(description="转向目标")
    parser.add_argument("-t", "--target", choices=["user", "table"], default="user",
                        help="table=转向桌子, user=转向用户")
    args = parser.parse_args()
    label = "用户" if args.target == "user" else "桌子"
    _run({"command": "turn-to", "target": args.target}, f"转向{label}")


def pick_main():
    parser = argparse.ArgumentParser(description="拾取物品（端起后默认处于高处）")
    parser.add_argument("target", choices=["cup", "bowl"], help="cup=端起水杯, bowl=端起碗勺")
    args = parser.parse_args()
    _run({"command": "pick", "target": args.target}, f"拾取{args.target}")


def place_main():
    parser = argparse.ArgumentParser(description="放置物品")
    parser.add_argument("target", choices=["cup", "bowl"], help="cup=放置水杯, bowl=放置碗勺")
    args = parser.parse_args()
    _run({"command": "place", "target": args.target}, f"放置 {args.target}")


def get_status_main():
    argparse.ArgumentParser(description="获取机器人当前状态").parse_args()
    _run({"command": "get_status"}, "获取状态")


def extend_hand_main():
    parser = argparse.ArgumentParser(
        description="伸出手臂 — 发送 JSON: {\"command\": \"extend-hand\", \"offset\": \"<value>\"}",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="示例:\n  extend-hand --offset high")
    parser.add_argument("--offset", choices=["low", "mid", "high"], default="mid",
                        help="伸手高度偏移 (默认: mid)\n"
                             "  low   - 低位偏移 (-0.10)\n"
                             "  mid   - 中间位置 (0)\n"
                             "  high  - 高位偏移 (+0.10)")
    args = parser.parse_args()
    _run({"command": "extend-hand", "offset": args.offset}, "伸出手臂")


def raise_hand_main():
    parser = argparse.ArgumentParser(
        description="抬起手 — 发送 JSON: {\"command\": \"raise-hand\", \"level\": \"<value>\"}",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="示例:\n  raise-hand --level significant")
    parser.add_argument("--level", choices=["slight", "moderate", "significant"], default="significant",
                        help="抬起幅度 (默认: significant)\n"
                             "  slight       - 轻微 (0.03)\n"
                             "  moderate     - 适中 (0.06)\n"
                             "  significant  - 大幅 (0.10)")
    args = parser.parse_args()
    _run({"command": "raise-hand", "level": args.level}, "抬起手")


def lower_hand_main():
    parser = argparse.ArgumentParser(
        description="放下手 — 发送 JSON: {\"command\": \"lower-hand\", \"level\": \"<value>\"}",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="示例:\n  lower-hand --level moderate")
    parser.add_argument("--level", choices=["slight", "moderate", "significant"], default="slight",
                        help="放下幅度 (默认: slight)\n"
                             "  slight       - 轻微 (0.03)\n"
                             "  moderate     - 适中 (0.06)\n"
                             "  significant  - 大幅 (0.10)")
    args = parser.parse_args()
    _run({"command": "lower-hand", "level": args.level}, "放下手")


def exec_trace_main():
    parser = argparse.ArgumentParser(
        description="执行轨迹移动 — 发送 JSON: {\"command\": \"exec-trace\", \"type\": \"<type>\", \"speed\": \"<speed>\", \"scale\": \"<scale>\", \"repeat\": N}",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="示例:\n  exec-trace --type up_and_down --speed fast --scale long --repeat 3")
    parser.add_argument("--type", choices=["up_and_down"], required=True, dest="trace_type",
                        help="轨迹类型 (必填)\n"
                             "  up_and_down - 上下抬升轨迹")
    parser.add_argument("--speed", choices=["slow", "normal", "fast"], default="normal",
                        help="移动速度 (默认: normal)\n"
                             "  slow   - 慢速 (0.8)\n"
                             "  normal - 正常 (1.0)\n"
                             "  fast   - 快速 (1.2)")
    parser.add_argument("--scale", choices=["short", "mid", "long"], default="mid",
                        help="移动幅度 (默认: mid)\n"
                             "  short - 短 (0.75)\n"
                             "  mid   - 中 (1.0)\n"
                             "  long  - 长 (1.25)")
    parser.add_argument("--repeat", type=int, default=1, metavar="N",
                        help="重复次数 (默认: 1)")
    args = parser.parse_args()
    _run({"command": "exec-trace", "type": args.trace_type, "speed": args.speed, "scale": args.scale, "repeat": args.repeat}, "执行轨迹移动")


def retract_hand_main():
    parser = argparse.ArgumentParser(
        description="收回手臂 — 发送 JSON: {\"command\": \"retract-hand\"}",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="示例:\n  retract-hand")
    parser.parse_args()
    _run({"command": "retract-hand"}, "收回手臂")


def list_trace_main():
    parser = argparse.ArgumentParser(
        description="列出所有可用轨迹类型 — 发送 JSON: {\"command\": \"list-trace\"}",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog="示例:\n  list-trace")
    parser.parse_args()
    _run({"command": "list-trace"}, "列出轨迹类型")
