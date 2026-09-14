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


def scoop_main():
    argparse.ArgumentParser(description="舀一勺食物").parse_args()
    _run({"command": "scoop"}, "舀一勺")


def deliver_spoon_main():
    parser = argparse.ArgumentParser(description="递送食物给用户")
    parser.add_argument("-u", "--user-id", required=True, help="用户 ID")
    args = parser.parse_args()
    _run({"command": "deliver-spoon", "user_id": args.user_id}, "递送")


def retract_spoon_main():
    argparse.ArgumentParser(description="撤回勺子").parse_args()
    _run({"command": "retract-spoon"}, "撤回勺子")
