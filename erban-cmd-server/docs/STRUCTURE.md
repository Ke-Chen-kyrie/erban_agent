# cmd_server 结构

> 命令执行服务 (FastAPI). 本文描述当前实际结构。接口/鉴权/执行语义见 [CONTRACT.md](CONTRACT.md), 加命令见 [../README.md](../README.md)。

如果拉不下来镜像 在 /etc/systemd/system/docker.service.d/http-proxy.conf配置代理

## 设计目标

1. 跨平台: x86_64 / aarch64 都能跑, `docker compose up` 即启
2. 高频变更物 (命令/库/启动脚本) 不烧进镜像, 挂载热改
3. 低频变更物 (server 本体/依赖) 留在镜像, 走构建缓存
4. 库与命令分离, 两者都可热改
5. 容器自身配置 (`config.yaml`) 独立 config/ 目录挂载, 不进镜像, 启动读一次, 缺失拒启
6. 命令单一事实源 = 该命令的 argparse (`build_parser`), 无 hand-written manifest

## 目录树

```
cmd_server/
├── Dockerfile              # 根 build context
├── docker-compose.yml
├── .dockerignore           # 排除 config 与 runtime
├── entrypoint.sh           # 启动脚本 (根, 单文件挂载/COPY 兜底, 见决策6)
├── pyproject.toml          # 项目根 (cmd_server 包安装源, 平铺布局)
├── cmd_server/             # 【进镜像】FastAPI 服务本体包 (低频改)
│   ├── main.py             # FastAPI 入口 /health /list /run + /catalog
│   ├── registry.py         # 扫描命令 + argparse introspection
│   ├── runner.py           # subprocess 执行
│   └── config.py           # server 自身配置 (白名单/system_commands/排除)
├── config/                 # 【挂载卷】容器自身配置, 不进镜像, 启动读一次
│   ├── config.yaml         # server 行为配置: allow_ips + system_commands + 排除 + robot/web/ident 段
│   └── .env                # 密钥 (IQS_API_KEY), gitignore, 不入库
└── runtime/                # 【挂载卷】客户端库 + 命令, 一挂到底 (热改)
    ├── robot_client/       # 机器人 WS 库 (自读 robot 段)
    │   └── commands/       # 机器人命令 *.py (每个含 build_parser)
    ├── web_client/         # 网络库搜索/天气/时间 (自读 web 段 + .env 密钥)
    │   └── commands/       # weather / cur-time / search
    ├── ident_client/       # 人脸识别库 (自读 ident 段)
    │   ├── camera.py       # Foxglove WS 抓帧
    │   └── commands/       # ident / detect
    └── emote_client/       # 表情播放库 (rosbridge, 自读 emote 段)
        └── commands/       # emote
```

各库的命令数不写死在文档里 (会过期), 看 `/catalog` 或 `ls runtime/*/commands/`。

compose 挂 config 卷 + runtime 卷 + entrypoint 单文件挂载, host 网络, 端口由 config 领:

```yaml
services:
  cmd-server:
    build: .
    container_name: erban-cmd-server   # 固定名 = 单实例: 重复 up 报冲突
    network_mode: host     # 端口即宿主端口, config.yaml server.port 领 (换端口不重建)
    environment:
      - CMD_CONFIG=/config
      - CMD_RUNTIME=/runtime
      - CMD_BIN=/usr/local/bin
    volumes:
      - ./config:/config                # 容器自身配置 (启动读一次, 缺失拒启)
      - ./runtime:/runtime              # 客户端库 + 命令
      - ./entrypoint.sh:/entrypoint.sh  # 入口单文件挂载 (热改)
    entrypoint: ["/bin/sh", "/entrypoint.sh"]
    restart: unless-stopped
```

镜像内置默认 `/entrypoint-default.sh` (COPY 根 entrypoint), 裸 `docker run` 无挂载时兜底也能起。

关于同名文件挂载: 单文件挂载仅用于**启动时一次性读取**的 entrypoint。**运行时持续读写的文件** (命令/库/配置) 一律目录挂载。

## 决策记录

### 1. 跨平台 (x86_64 / aarch64)

- 基础镜像用官方多架构 `python:3.12-slim` (eclipse-zenoh 尚未出 3.13 wheel, 锁 3.12)
- 依赖一律选**两架构都有 wheel** 的包
- 本仓库代码全纯 Python (client 库 / server / 命令脚本), 无宿主架构依赖
- `config.yaml` 的 ip/port 走 host 网络或桥接网络, 与架构无关

### 2. Flask → FastAPI

换用 FastAPI。理由: Pydantic 自动请求体校验、自动 OpenAPI、原生异步 (subprocess 阻塞用同步 `def` 端点跑线程池)。

注意: `subprocess.run` 是阻塞 — 端点是同步 `def`, 不要 `async def` 里直接跑 subprocess。

### 3. 配置归属: server 关切 vs client 配置

**分层归属按"谁消费"**, 不按"在哪定义":

| 配置 | 归属 | 谁读 | 读时机 |
|---|---|---|---|
| `allow_ips` / `system_commands` / `exclude_scan_paths` | server | server (`cmd_server/config.py`) | 启动读一次 |
| `catalog.enable_test` | server | server (`cmd_server/config.py`) | 启动读一次 |
| `server.port` | server | entrypoint (python 一行读) | 启动读一次 |
| `robot` 段 (host/port/timeout) | robot_client | robot_client (`runtime/robot_client`) | 命令运行时 |
| `web` 段 (iqs_endpoint) + `.env` IQS_API_KEY | web_client | web_client | 命令运行时 |
| `ident` 段 (camera_ws_url/topic/identification_url) | ident_client | ident_client | 命令运行时 |

- **server 只读 server 关切**: `cmd_server/config.py` 解析 allow_ips/system_commands/exclude_scan_paths, 不碰 robot/web/ident
- **client 配置 client 读**: 三个库各自 `yaml.safe_load(/config/config.yaml)` 取所需段 (`$CMD_CONFIG` 覆盖根); `.env` 密钥由 web_client 解析 (无 python-dotenv)
- **密钥进 `.env`**: `IQS_API_KEY` 存 config/.env (gitignore), server 不注入命令 env — 子进程继承 os.environ, 库自读
- **单一配置文件**: robot/web/ident 段共享 config/config.yaml (多个抠到一段, 挂载同一目录)
- **缺失即拒启**: entrypoint 启动先查 config.yaml 存在, 缺 `server.port` 也拒 — 宁起动失败, 不裸奔/默认
- **缺 config.yaml 时 server 兜底全拒** (allow_ips 空 = 全拒, 宁拒勿放), 双保险

### 4. runtime 结构: 库即目录 (无独立 feature 包) + manifest 废弃

**库与命令合并到同一个 client 库目录, 删除功能分组** (原 feeding/navigation/rehab/info 并入实现它的 client 库)。三个库平铺在 runtime 根, 每个带 `commands/`:

```
runtime/robot_client/commands/*.py     # 引 from robot_client import ...
runtime/web_client/commands/*.py       # 引 from web_client import ...
runtime/ident_client/commands/*.py     # 引 from ident_client import ...  (+ camera)
```

- **命令单一事实源 = argparse**: 每个命令模块暴露 `build_parser() -> argparse.ArgumentParser`; registry 调它 introspection `parser._actions` 出描述与参数 (name/alias/required/type/choices/default/help)。**无 hand-written `<name>.json` manifest**
- **`help=` 串约定**: 只写参数语义, **不重复默认/必填/choices** —— 这些由渲染器 `cmd_server/main._param_meta` 自动出 `必填`/`[choices]`/`默认 X` 标签。choices 取值语义用 `X=说明`, 逗号+空格分隔 (`cup=水杯, bowl=碗勺`); 括号写半角 `()`、括号前留空格 (`搜索词 (多词用空格拼合)`, `平移距离 (米)`); 中英/中数间不加空格 (`用户ID`, `覆盖level`)。仅在默认值有回调用法时才写默认 (如 `循环次数 (0=跳过)`, 渲染器已另行打 `默认 1`)
- argparse `option_strings` 自带别名 (`-d` + `--direction`), type 只映射 int/float→"数字" 其余"字符串"; 位置参数 (search query / weather city) 视作必填
- **真隐藏参数 = 不进 build_parser**: argparse `--help` 全量展示, agent 可调 — 想对 agent 藏的参不 add_argument, server 流程内部设置
- PYTHONPATH 启动时设 `export PYTHONPATH="$CMD_RUNTIME"` → 顶层三客户端库均可 import, 命令 `from robot_client import ...` 成立
- 命令发现: `_find_features`: `runtime/*` 中带 `commands/` 子目录者, glob `*/commands/*.py`; 命令名 = 文件名 (全仓唯一)
- 新命令流程: 写 `runtime/<client>/commands/<name>.py` (含 build_parser) → 调 `GET /list`, 不重启容器
- 命令名/参数名与机器人端 `{"command": ...}` payload 字段一一对应

### 5. 依赖分层 + 缓存 (构建期)

- 依赖不进 runtime, **构建时 `pip install`**, 变更 = 重建镜像
- 单一来源 = Dockerfile 的 `RUN` 行本身, 不设 requirements.txt
- 版本号内联 RUN 行; 逐 RUN 一包一层, 越稳定越靠前, 新依赖尾行追加; 禁改既有行 (一处动后续全失效)
- server 本体最后 COPY:

```dockerfile
COPY pyproject.toml /app/cmd_server/            # 项目根
COPY cmd_server/ /app/cmd_server/cmd_server      # 包 (平铺: 根含 pyproject + cmd_server/ 子包)
RUN pip install --no-cache-dir -e /app/cmd_server
```

- **平铺布局要点**: editable install 需项目根 (有 pyproject.toml) 与 `cmd_server/` 子包分离; pyproject 放仓库根, 包代码在 cmd_server/ 子目录。若 pyproject 与包模块同目录则 find 发现不到包, import 失败 (`No module named 'cmd_server'`)

预置依赖: eclipse-zenoh==1.5.1 / zenoh-ros2-sdk / rosbags (协议), fastapi==0.115.8 / uvicorn / gunicorn / httpx / websocket-client / websockets / pyyaml (运行时)。

### 6. entrypoint.sh 根目录 + 单文件挂载 + 镜像兜底

- entrypoint 常改, 放**根目录** `entrypoint.sh`, compose 单文件挂载, 改它免重建
- 镜像内置 `/entrypoint-default.sh` 兜底
- 单文件挂载仅限启动一次性读取的 entrypoint; 运行时持续读写的文件一律目录挂载

### 7. 白名单: 注册表 + 系统命令 (两源合并)

白名单 = 注册表本身, 无独立文件:

1. **扫描命令**: `runtime/*/commands/*.py` 文件名 → 命令名
2. **系统命令**: `config/config.yaml` `system_commands` 读入合并 (基础 shell 工具, 非机器人命令)

`/run` 校验: 按 `| ; && ||` 切成链后 (见决策10), **每段首 token 各自** ∈ (命令名 并 系统命令), 任一段不合即整条拒 (前半段也不跑)。IP 白名单 `allow_ips` 缺省全拒, `127.0.0.1` 恒兜底; 匹配用 `ipaddress` (单机/网段)。

### 8. 启动顺序与 worker

**boot 顺序 (config 只启动读一次):**

1. entrypoint 查 config.yaml 存在且含 `server.port`, 缺失**拒绝启动**
2. server `app = create_app()` (模块导入时): 读 config (allow_ips/system_commands/排除) + `registry.rescan()` boot 扫
3. gunicorn bind `0.0.0.0:$server.port`

config 不改热读: 改 allow_ips/system_commands 必重启。

**worker: gunicorn 2 worker + uvicorn worker (ASGI)**:

```
gunicorn --worker-class uvicorn.workers.UvicornWorker --workers 2 cmd_server.main:app
```

- **必须用 uvicorn worker**: gunicorn 默认 sync worker 是 WSGI, 调 FastAPI(ASGI) 直接崩 (`FastAPI.__call__() missing 'send'`)
- uvicorn worker 自带线程池跑同步 `def` 端点; `--threads` 与 uvicorn worker 不兼容, 不要加

### 9. 自身接口

| 端点 | 方法 | 作用 |
|---|---|---|
| `/health` | GET | 存活 + 命令数 |
| `/list` | GET | 重扫 (重建注册表 + 重生成 shell 包装), 返回命令名+描述清单 (参数 schema 在 /catalog, 这里只回名+一行) |
| `/catalog` | GET | **人类可读命令目录页** (include_in_schema=False, 不进 Swagger); 顶部可切复制格式 (全/仅长参/仅短参) + 列 checkbox (描述/参数/示例), 生成可勾选列 Markdown 命令表进文本框架, 选中即原生复制 (不依赖 clipboard API, 手机平板可用) |
| `/run` | POST | 解析命令串 (含管道链), 每段按注册表校验白名单, 执行 `{"cmd": "..."}` |

- `/catalog` 不进 Swagger: 命令集是 `/run` 的参数内容, 不该污染 docs; 人类看叫的人用 catalog
- `/catalog` 命令表: `data` 预渲染 24 键 (`{full|long|short}` × 列布尔键 `{000..111}`), 命令列恒在, 描述/参数/示例三列服务端按勾选态拼平文本 (浏览器端剥不了 markdown 列); JS 按格式 select + 3 checkbox 联选重填 textarea, 文本框直接选中原生复制 (无 clipboard 按钮, LAN http 下 `navigator.clipboard`/`execCommand` 移动端不稳故弃)
- `/catalog` 测试命令: `catalog.enable_test` 开关 (默认 false, 宁拒勿放), 开时浏览表每行加"测"按钮 (data-cmd 存短参骨架含必填占位 `<值>`/choices 与可选 `[--flag]`, html-escape 防属性注入), 点击填进命令输入框; 发送 `fetch POST /run` 同步等回复, `confirm()` 二次确认 (物理动作); 关掉时整个测试 UI 不渲染。可选参非全有效, 故用骨架让用户自选删留, 不预填默认
- `/catalog` 示例约定 `<b>尖括号=必填值</b>, 方括号=可选</b>` (POSIX)
- `/list` 只回命令**名 + 一行描述** (省调用方上下文, 描述帮忙选命令); **不含参数 schema** —— 正确调参的源是 `/catalog` (人工复制或现查), 不是 `/list`。命令集取 `[].name`; system_commands (基础 shell 工具) 不抖 (仍在 `/run` 鉴权)
- `/run` 鉴权失败 error 只报缺失命令名, 不内联命令列表 (命令集从 `/list` 取)
- `/run` 阻塞到机器人返回; 允许并行发出, 并发/乱序交给**机器人侧拦截**
- `/run` 鉴权: IP 白名单

包装逻辑 (entrypoint 启动时 + 每次 /list): 每个命令写 `/usr/local/bin/<name>` 包装脚本 (tmp+mv 原子写), `exec python3 <py> "$@"`。

### 10. 命令串: shlex 切链 + 每段各自鉴权

`/run` 的 `cmd` 是**有限 shell 子集**, 不是裸 shell:

- 解析在 `runner.parse_chain`: `shlex(punctuation_chars=True)` 切 token (`;()<>|&` 连续标点合成一个 token, 故 `&&`/`>>` 是单 token), 再按 `| ; && ||` 归成"管道链"结构 `[(前置连接符, [argv...]), ...]`
- **只放行 `| ; && ||`**, 其余标点 token (`> >> < << & ( )`) 一律 400 —— 重定向 = 任意写文件, 后台/子 shell = 逃逸面, 与白名单是两回事
- 解析后**先整条解析成 argv 再执行**: 任一段首 token 不在白名单即整条 400, 前半段不跑 (不会"跑一半发现越权")
- 执行在 `runner.run_chain`: 管道真接 stdin/stdout, 中间段 stderr 汇入临时文件 (无 shell 可借, 自己接, 免得中间段写满管道阻塞); `&&`/`||` 短路
- 全程 `shell=False`, 无变量展开/通配符/命令替换; 引号按 shell 规则解析, 引号内的 `|` 是字面参数 (与 shell 一致)
- 代价: 白名单外的能力依然拿不到, 只省调用方多轮往返; 链路超时是整条总预算

## 未定 / 待议

- `/list` 的命令名/参数面向 LLM 发现; agent 的调用规范 (含流程) 由使用方自己写, 命令表从 `/catalog` 复制, 本项目不维护 skill 文件
- 若未来某功能需独立权限/隔离, 再按功能拆分目录