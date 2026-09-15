# CLAUDE.md — 如何给 cmd_server 添加功能

> 本仓库 = 一个基于 FastAPI 的命令执行容器/沙箱。命令挂 `runtime/` 卷热更, **免重建镜像**。
> 详细结构见 [STRUCTURE.md](docs/STRUCTURE.md), 接口/鉴权/白名单/执行语义见 [CONTRACT.md](docs/CONTRACT.md), 分步示例见 [README.md](README.md)。此处只压出加功能的操作规范。
> 本仓库目标用途是部署在机器人(naviai@192.168.217.100)上, 但是也可以部署在其他地方进行测试, 只要能连接到相应服务端就能正常使用

## 一条命令 = 一个文件

```
runtime/<client>/commands/<name>.py
```

- **命令名 = 文件名**, 全仓唯一; snake_case, 不缩写
- 依赖运行时热挂载: 写好文件 → 调 `GET /list` 触发重扫 → 即可 `POST /run`, **不重启容器**
- 命令被包装成 `bin/<name>` 脚本, `exec python3 "<py>" "$@"` 调起, 故：

```python
"""<一句话说明 做什么>"""

import argparse
import sys
from <client> import <public_fn>   # 命令逻辑在 client 库, 脚本只调

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="<人类可读一句话>")
    parser.add_argument(...)
    return parser

def main() -> int:
    args = build_parser().parse_args()
    print(<result>)   # stdout = 结果
    return 0          # 退出码 = 成功/失败, 必须返回

if __name__ == "__main__":
    sys.exit(main())   # 末行恒在
```

**每条命令必须有 `build_parser()` + `description` + 参数 `help=`。** `build_parser` 是命令的单一事实源 —— registry introspection 它出 `/list` 的描述和 `/catalog` 的参数表, 没有就要补上, 不许空手提交命令。

## 放哪个 client 库

| 命令类型 | 放哪 | 连哪 |
|---|---|---|
| 控制机器人 | `runtime/robot_client/commands/` | 机器人 WS: `run_robot_command({...}, label)`, payload 字段与机器人端一一对应 |
| 查外部网络 | `runtime/web_client/commands/` | Web API, 同步返回文本 |
| 人脸/感知识别 | `runtime/ident_client/commands/` | 摄像头抓帧 + 识别/关节服务 API |
| 三类都不合 | 新建 `runtime/<client>/commands/` | registry 自动发现 (带 `commands/` 子目录即客户端库) |

逻辑写进 client 库公开函数 (`runtime/<client>/__init__.py`), 命令脚本薄封装。命令级连接配置也归 client 库读。

## 配置归属 (最容易踩的坑)

按**谁消费**分层, 不按写在哪:

- server 只读 `cmd_server/config.py`: `allow_ips` / `system_commands` / `exclude_scan_paths` / `catalog.enable_test`。**加命令不要动这里。**
- client 配置 client 自读 `config.yaml` 对应段 (`robot` / `web` / `ident`), `$CMD_CONFIG` 覆盖根。**新增端点 → 加进所属段的 `config.yaml`, 在 client 库 `__init__.py` 加读取 helper, 命令调 helper。绝不硬编码进命令文件。**（例:`focus-joint` → `ident.joint_url`, helper `ident_client.joint_url()`。）
- 密钥进 `config/.env` (gitignore), 库自读, 不硬编码。

## 参数 `help=` 约定

- 只写参数语义, **不重复默认/必填/choices** —— 渲染器自动出 `必填`/`[choices]`/`默认 X` 标签
- choices 取值语义用 `X=说明`, 逗号+空格分隔: `cup=水杯, bowl=碗勺`
- 括号半角 `()`、括号前留空格 (`平移距离 (米)`); 中英/中数间不加空格 (`用户ID`、`覆盖level`)
- 真隐藏参数 = 不进 `build_parser` (argparse `--help` 全量展示, agent 看得到)

## 依赖

已装: `fastapi` `uvicorn` `gunicorn` `httpx` `websocket-client` `websockets` `pyyaml` `eclipse-zenoh` `zenoh-ros2-sdk` `rosbags`。**先用现成的** (例子里 ident_client 用 httpx 走 POST, 别为同样的事引 aiohttp/新包); 真要加 → Dockerfile 末尾追加一行 `RUN pip install ...`, 重建镜像, 禁改既有行。

## 热更 vs 必重启

| 改什么 | 要做什么 |
|---|---|
| `runtime/` 命令/库 | 热更, 只触发重扫 (`GET /list`) |
| `config.yaml` | 重启容器 (启动读一次) |
| `cmd_server/*.py` | 重建镜像 |

## 验证

```bash
curl -s http://<主机>:<server.port>/list   # 新命令在 added 里
curl -s -X POST http://<主机>:<server.port>/run -d '{"cmd":"<命令> <参数>"}'
curl -s -X POST http://<主机>:<server.port>/run -d '{"cmd":"no-such-command"}'  # 负向 400
```
(`ls`/`cat`/`grep` 这些在 `system_commands` 白名单里, 是合法命令, 不是负向用例。)

新增/改动后跑一次 registry introspection `_inspect_command` 确认描述+参数出现, 别只 `py_compile`。