# Robot CLI 工具集

机器人物理运动与摄像头控制 CLI 命令集合，每个命令独立打包，按需安装。

## 安装

```bash
# 1. 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2. 安装共享库（必须，含 turn/pick/place/get-status 命令）
pip install -e cli/robot-common

# 3. 按需安装命令（可全部安装）
pip install -e cli/water
pip install -e cli/food
pip install -e cli/ident
pip install -e cli/search
pip install -e cli/weather
pip install -e cli/cur_time

# 或一次性安装全部
pip install -e cli/{water,food,ident,search,weather,cur_time}

# 4. 加载环境变量
export $(cat .env | grep -v '^#' | xargs)

# 5. （可选）安装 HTTP API 服务器
pip install -e cli/server
```

## 启动

```bash
# 加载环境变量（每次新终端都要执行）
export $(cat .env | grep -v '^#' | xargs)

# CLI 模式：直接运行命令
turn -t table
pick cup
lower-cup
deliver-cup -u zhouziqi
search 你好

# 服务模式：启动 HTTP API（开发用 Flask 内置服务器）
robot-api                          # 监听 :8080
```

## Docker 部署

```bash
  cd server && docker compose up --build -d
```

容器启动后在 `8080` 端口提供 HTTP API 服务，环境变量通过 `.env` 文件统一配置。

## HTTP API

安装 `server` 模块或通过 Docker 启动后，可通过 HTTP 远程执行机器人命令。

**`POST /run`**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| cmd | string | 是 | 要执行的 Shell 命令 |

请求：

```bash
curl -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "turn -t user"}'
```

响应：

```json
{"output": "...", "success": true}
```

## 测试

```bash
# 加载环境变量
export $(cat .env | grep -v '^#' | xargs)

# === 共用机器人命令 ===
curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "turn -t table"}'

curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "turn -t user"}'

curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "pick cup"}'

curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "place cup"}'

curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "get-status"}'

# === 喂水 ===
curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "lower-cup"}'

curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "deliver-cup -u zhouziqi"}'

curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "retract-cup"}'

# === 喂饭 ===
curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "scoop"}'

curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "deliver-spoon -u zhouziqi"}'

curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "retract-spoon"}'

# === 人脸验证 ===
curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "ident -u zhouziqi"}'

# === 联网搜索 ===
curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "search 机器人最新技术"}'

# === 天气查询 ===
curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "weather 杭州"}'

# === 时间查询 ===
curl -s -X POST http://localhost:8080/run \
  -H "Content-Type: application/json" \
  -d '{"cmd": "cur-time"}'
```

## 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ROBOT_HOST` | `192.168.217.100` | 机器人 WebSocket 地址 |
| `ROBOT_PORT` | `9092` | 机器人 WebSocket 端口 |
| `ROBOT_TIMEOUT` | `180.0` | 命令执行超时（秒） |
| `CAMERA_WS_URL` | `ws://192.168.217.100:8768` | Foxglove WebSocket Bridge 地址 |
| `CAMERA_TOPIC` | `/zj_humanoid/sensor/realsense_head/color/image_raw/compressed` | 头部摄像头 topic |
| `IDENTIFICATION_URL` | `http://192.168.217.100:8001` | 人脸识别服务地址 |
| `IQS_API_KEY` | — | 阿里云 IQS 搜索 API Key |
| `IQS_ENDPOINT` | `https://cloud-iqs.aliyuncs.com/search/unified` | 阿里云 IQS API 地址 |

## 命令列表

### 共用机器人命令（robot-common 包）

| 命令 | 说明 |
|------|------|
| `turn -t user\|table` | 转向目标 |
| `pick cup\|bowl` | 拾取物品（端起后默认处于高处） |
| `place cup\|bowl` | 放置物品 |
| `get-status` | 获取机器人当前状态 |

### 喂水（water 包）

| 命令 | 说明 |
|------|------|
| `lower-cup` | 放低水杯 |
| `deliver-cup -u <用户ID>` | 递送水杯给用户 |
| `retract-cup` | 撤回水杯（撤回后回到高处） |

### 喂饭（food 包）

| 命令 | 说明 |
|------|------|
| `scoop` | 舀一勺食物 |
| `deliver-spoon -u <用户ID>` | 递送食物给用户 |
| `retract-spoon` | 撤回勺子 |

### 摄像头

| 命令 | 说明 |
|------|------|
| `ident -u <用户ID>` | 检查指定用户是否在摄像头画面中 |

### 信息查询

| 命令 | 说明 |
|------|------|
| `search <关键词>` | 联网搜索，返回 5 条结果摘要 |
| `weather <城市>` | 查询指定城市的实时天气和 7 天预报 |
| `cur-time` | 获取当前日期和时间 |

### 工具函数

`robot_common` 提供 `run_shell(cmd)` 函数，执行 Shell 命令并返回输出。

```python
from robot_common import run_shell

result = run_shell("turn -t user")
print(result)
```

## 使用示例

```bash
# 加载环境变量
export $(cat .env | grep -v '^#' | xargs)

# 喂水流程
turn -t table && pick cup && turn -t user && lower-cup && deliver-cup -u zhouziqi && retract-cup && turn -t table && place cup

# 喂饭流程
turn -t table && pick bowl && turn -t user && scoop && deliver-spoon -u zhouziqi && retract-spoon && turn -t table && place bowl

# 人脸验证
ident -u zhouziqi

# 信息查询
search 机器人最新技术
weather 杭州
cur-time

# 自定义机器人地址
ROBOT_HOST=192.168.1.100 turn -t user
```

## 目录结构

```
cli/
├── .env                         # 环境变量配置
├── .gitignore
├── robot-common/                # 共享库（必须安装）
│   └── robot_common/
│       ├── __init__.py          # env() + run_shell() + IQS API
│       ├── client.py            # RobotClient WebSocket 客户端
│       ├── camera.py            # Foxglove 摄像头帧捕获
│       └── commands.py          # 共用 CLI: turn/pick/place/get-status
│
├── water/                       # 喂水: lower-cup/deliver-cup/retract-cup
├── food/                        # 喂饭: scoop/deliver-spoon/retract-spoon
├── ident/                       # 人脸验证
├── search/                      # 联网搜索（IQS API）
├── weather/                     # 天气查询（IQS API）
├── cur_time/                    # 时间查询
└── server/                      # HTTP API 服务器（Docker 部署）
    ├── Dockerfile
    ├── docker-compose.yml
    └── server/
        └── __init__.py          # POST /run 接口
```

## 返回值

- 成功：退出码 `0`，stdout 输出结果信息
- 失败：退出码 `1`，stderr 输出错误信息
