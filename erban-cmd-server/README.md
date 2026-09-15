# cmd_server 添加新命令

> 给 cmd_server 加一条命令的分步指南。接口/鉴权/白名单/执行语义见 [CONTRACT.md](docs/CONTRACT.md); 结构设计见 [STRUCTURE.md](docs/STRUCTURE.md)。命令文件全部挂在 `runtime/` 卷, **热更免重建镜像**。

## 总览

一条命令 = 一个 `runtime/<client>/commands/<name>.py` 文件。命令名 = 文件名 (全仓唯一)。文件暴露 `build_parser()` (argparse, 命令的单一事实源: 描述/参数/choices/默认/help 全从它 introspection), 和 `main()` (真的执行)。

三步走:

1. 写命令文件 (含 `build_parser`)
2. 触发重扫: `GET /list` 或浏览器开 catalog 点"重扫命令"
3. `POST /run` 验证

不用重启容器, runtime 卷热挂。唯一要重启的: 改了 server 本体 (`cmd_server/*.py`) 或 `config.yaml`。

## Step 1: 建文件

按命令类型选客户端库目录 (决定命令执行时连哪):

| 命令类型 | 放哪 | 连哪 |
|---|---|---|
| 控制机器人 (运动/喂食/康复) | `runtime/robot_client/commands/` | 机器人 WS, 发 JSON 命令 |
| 查外部网络 (搜索/天气/时间) | `runtime/web_client/commands/` | Web API |
| 人脸识别 | `runtime/ident_client/commands/` | 摄像头抓帧 + 识别 API |

**新功能分类** (三类都不合): 在 `runtime/` 下新建 `<client>/commands/` 目录即可, registry 自动发现 (有 `commands/` 子目录的 `runtime/*` 都是客户端库)。

## Step 2: 写命令文件

三份模板任选其一, 照着自己库的公开接口填。

### 机器人命令 (robot_client)

```python
"""给机器人发 {"command": "<动作名>"} 的说明一句话."""

import argparse
import sys

from robot_client import run_robot_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="<人类可读一句话: 做什么>")
    parser.add_argument(
        "-t",
        "--target",
        choices=["cup", "bowl"],
        required=True,
        help="cup=水杯, bowl=碗勺",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    run_robot_command({"command": "pick", "target": args.target}, f"拾取{args.target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

按需调: `robot_client.run_robot_command(command_dict, label)` 是 `RobotClient.send_command(...)` 的薄封装 (自读 `config.yaml` 的 `robot` 段连 WS, 发 JSON, 带超时/失败处理)。**`command` 字典里的字段名与机器人端 payload 字段一一对应**, 先核对好再写。

### 网络命令 (web_client)

```python
"""查询搜索结果的说明."""

import argparse
import sys

from web_client import search_web


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="<人类可读一句话>")
    parser.add_argument("query", nargs="+", help="搜索词 (多词用空格拼合)")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    print(search_web(" ".join(args.query)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

网络命令是同步返回文本: `print(结果)` 写 `stdout`, `build_parser` 里不跑网络。密钥走 `config/.env` (`IQS_API_KEY`), 由 `web_client` 自读, **别硬编码进命令文件**。

### 识别人命令 (ident_client)

```python
"""X人脸识别的说明."""

import argparse
from pathlib import Path
import sys

from ident_client import camera_topic
from ident_client import camera_ws_url
from ident_client import verify_face
from ident_client.camera import capture_ros_frame


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="<人类可读一句话>")
    parser.add_argument("-u", "--user-id", required=True, help="用户ID")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    frame_path = capture_ros_frame(camera_ws_url(), camera_topic())
    if not frame_path:
        print("无法获取画面, 请检查摄像头连接", file=sys.stderr)
        return 1
    try:
        result = verify_face(args.user_id, frame_path)
        matched = bool(result.get("match", False))
        print(f"用户 {args.user_id} 匹配: {matched}")
        return 0 if matched else 1
    finally:
        Path(frame_path).unlink(missing_ok=True)  # 用完删临时帧


if __name__ == "__main__":
    sys.exit(main())
```

点位: 末行 `if __name__ == "__main__": sys.exit(main())` 恒在, 命令以 `bin/<name>` 包装脚本被 `/run` 调起, 靠它取 `main()` 返回码。

## build_parser 约定

- **`description`**: 一句话, 成为 `/list` 与 catalog 里该命令的人类可读描述 (无则回退取模块 docstring 首行)。
- **参数**: 选项用 `add_argument("-x", "--long-name", ...)`; 位置参数 (如搜索词) 用 `nargs="+"` 且必填。真隐藏参数**不进** build_parser (不对 agent 暴露)。
- **`help=` 串约定**: 只写参数语义, **不重复默认/必填/choices** —— 渲染器自动出 `必填`/`[choices]`/`默认 X` 标签。choices 取值语义用 `X=说明` (`cup=水杯, bowl=碗勺`); 括号写半角 `()`、括号前留空格 (`平移距离 (米)`); 中英/中数间不加空格 (`用户ID`, `覆盖level`)。默认值仅在回调用法有意义时才写 (如 `循环次数 (0=跳过)`, 渲染器已另行标 `默认 1`)。
- 命名不缩写; 命令名/参数名 snake_case (全仓统一)。

## Step 3: 生效

runtime 卷热挂, 新文件即扫即用:

```bash
# 触发重扫 (重建注册表 + 重新生成 /usr/local/bin/<name> 包装脚本)
curl -s http://<主机>:<server.port>/list
# 或浏览器开 catalog 页, 点顶部"重扫命令"
```

返回 `refresh: {added, removed, changed, command_count}` 确认新命令 `added` 里。

## Step 4: 验证

```bash
# 健康 + 命令数
curl -s http://<主机>:<server.port>/health
# /list 里能查到新命令 (名 + 描述)
curl -s http://<主机>:<server.port>/list
# 跑一条 (真执行, 动作型请确认安全再发)
curl -s -X POST http://<主机>:<server.port>/run -H 'Content-Type: application/json' \
  -d '{"cmd":"<新命令> <参数>"}'
# 不在白名单 → 400 (必须带 Content-Type, 否则 422 参数校验错)
# 注意: ls/cat/grep 这些在 system_commands 里, 是合法的, 不能拿来当负向用例
curl -s -X POST http://<主机>:<server.port>/run -H 'Content-Type: application/json' \
  -d '{"cmd":"no-such-command"}'
```

进容器直接跑命令 (排查用, 包装脚本在 `/usr/local/bin`, 已在 PATH):

```bash
# 必须带 PYTHONPATH: 包装是 python3 /runtime/<client>/commands/<name>.py,
# sys.path 只含脚本所在目录, 不加则 from <client> import ... 报 ModuleNotFoundError
docker exec -it -e PYTHONPATH=/runtime erban-cmd-server bash
# 进去后直接打命令名
deliver --help
```

## 约定

- **命令名全仓唯一**: 重命名/冲突某一条失败, 不影响整体扫描 (坏命令回退空描述空参数, 但仍注册)。
- 参数名与机器人端 payload 字段一致, 不一致会发错命令。
- `config.yaml` 的 `exclude_scan_paths` 里的目录不扫描 (如 `runtime/examples/`)。
- `system_commands` (如 `ls`) 是配置里的基础 shell 工具白名单, **不是命令文件**, 与命令名两源合并进白名单。`/run` 的 `cmd` 支持 `| ; && ||` 管道链, 每段各自过白名单 (见 CONTRACT)。
- 网络命令同步阻塞; 机器人命令阻塞到返回 (HTTP 响应即结果), 物理动作期间别并发发命令。
- 改了服务端本体或 config 才需重建/重启: `docker compose up -d --build`。
- catalog 页能直接浏览器测新命令 (开 `catalog.enable_test` 时), 见 CONTRACT。