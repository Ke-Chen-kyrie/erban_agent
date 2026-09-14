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


def lower_cup_main():
    argparse.ArgumentParser(description="放低水杯").parse_args()
    _run({"command": "lower-cup"}, "放低水杯")


def deliver_cup_main():
    parser = argparse.ArgumentParser(description="递送水杯给用户")
    parser.add_argument("-u", "--user-id", required=True, help="用户 ID")
    args = parser.parse_args()
    _run({"command": "deliver-cup", "user_id": args.user_id}, "递送水杯")


def retract_cup_main():
    argparse.ArgumentParser(description="撤回水杯（撤回后回到高处）").parse_args()
    _run({"command": "retract-cup"}, "撤回水杯")
