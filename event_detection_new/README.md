# Event Detection

面向养老监护场景的实时视频事件检测服务。系统从摄像头、ROS/Foxglove 或 Zenoh 持续获取图像，通过 OpenAI 兼容的视觉语言模型识别异常事件，并把检测结果推送到 Erban 大屏和外部 Agent。

生产环境推荐直接拉取已经构建好的阿里云镜像：

```text
guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:20260904
```

新机器不需要下载源码或重新构建镜像，只需准备 `.env`、日志目录和结果目录，然后通过 `docker run` 创建容器。大屏后续使用 `docker start` 和 `docker stop` 控制该容器。

镜像由**生产运行的机器人侧**发布（不在 amd64 开发机推送，架构不匹配），当前生产 tag 以最后一次发布为准；发布流程与重复推送的覆盖规则见「镜像发布到阿里云」。

## 主要功能

- 支持本地摄像头、Foxglove/ROS2、ROS1 rosbridge 和 Zenoh 图像源。
- 检测挥手、胸痛、头痛、腹痛、揉肩、OK、点赞和跌倒等事件。
- 疼痛类和跌倒事件采用二次确认机制：先延迟数秒，再用最近几帧复核，确认通过才推送，降低误触。
- 图像采集与模型推理解耦，只保留最新帧，避免推理变慢后堆积历史画面。
- 支持可选的中期人物行为摘要和长期记忆压缩。
- 向独立的 `erban_dashboard` 推送检测事件和可选的实时画面。
- 向外部 Agent Webhook 独立推送事件。

## 数据链路

```text
Camera / Foxglove / rosbridge / Zenoh
                    │
                    ▼
                 main.py
          图像采集 + 最新帧缓冲
                    │
                    ▼
          webinfer/live_adapter.py
           主视觉模型事件检测
             │             │
             │             ├──► Erban Dashboard
             │             └──► Agent Webhook
             │
             ├──► 中期人物行为摘要
             └──► 长期记忆压缩
```

事件检测服务和大屏可以使用不同的视频源。例如，事件检测从 Zenoh 获取图像，大屏可以独立订阅机器人 ROS2 图像话题。

## 部署方式概览

| 场景 | 推荐方式 |
|---|---|
| 新机器生产部署 | 从阿里云拉取镜像，然后执行一次 `docker run` |
| 日常启动和停止 | 大屏按钮，或手动执行 `docker start` / `docker stop` |
| 修改 `.env` | 删除旧容器，使用相同镜像重新执行 `docker run` |
| 更新程序版本 | 拉取新 tag，删除旧容器，使用新 tag 重新执行 `docker run` |
| 本地开发调试 | `uv sync` 后执行 `uv run python main.py` |

重要约定：

- `.env` 不在镜像内，只在创建容器时通过 `--env-file` 注入。
- `docker start` 和 `docker restart` 不会重新读取 `.env`。
- `docker run` 会创建一个新容器并立即启动；同名容器已经存在时，再次执行会报名称冲突。
- 容器固定命名为 `event-detection`，以便大屏识别并控制。
- 容器不设置自动重启策略，由大屏管理生命周期。

## 新机器生产部署

### 1. 安装并启动 Docker

确认 Docker 可用：

```bash
docker version
docker info
```

当前登录用户必须有权限访问 Docker。如果只能通过 `sudo docker` 使用，还需要处理大屏服务用户的 Docker 权限，否则大屏无法一键启停事件检测容器。

### 2. 登录阿里云容器镜像服务

```bash
docker login guopeilin-registry.cn-hangzhou.cr.aliyuncs.com
```

根据提示输入阿里云容器镜像服务用户名和访问凭证。不要把密码或访问令牌写入 README、`.env` 或脚本。

### 3. 拉取镜像

```bash
docker pull guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:20260904
```

验证镜像已下载：

```bash
docker image inspect \
  guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:20260904
```

### 4. 准备运行目录

下面以 `/opt/erban/event_detection` 为例。也可以使用其他绝对路径，但大屏配置和 Docker 挂载路径必须同步修改。

```bash
sudo mkdir -p /opt/erban/event_detection/logs
sudo mkdir -p /opt/erban/event_detection/result
sudo mkdir -p /opt/erban/event_detection/certs
# Langfuse HTTPS 入口使用自签证书，需将证书复制到 certs/（.env 中
# OTEL_EXPORTER_OTLP_CERTIFICATE=certs/langfuse-erban-cert.pem 引用它）
sudo cp /path/to/langfuse-erban-cert.pem /opt/erban/event_detection/certs/
sudo chown -R "$USER":"$USER" /opt/erban/event_detection
cd /opt/erban/event_detection
```

目录结构：

```text
/opt/erban/event_detection/
├── .env
├── certs/
│   └── langfuse-erban-cert.pem
├── logs/
└── result/
```

`.env` 包含模型地址、API Key、视频源和事件推送地址。建议从受控的部署配置或旧机器安全复制，不要将包含密钥的 `.env` 提交到 Git。

### 5. 首次创建并启动容器

使用 Zenoh、Foxglove 或 rosbridge 等网络图像源时：

```bash
cd /opt/erban/event_detection

docker run -d \
  --name event-detection \
  --network host \
  --env-file .env \
  -v "$PWD/logs:/app/logs" \
  -v "$PWD/result:/app/result" \
  -v "$PWD/certs:/app/certs" \
  guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:20260904
```

使用本机摄像头且 `VIDEO_SOURCE=camera` 时，还需要映射摄像头设备：

```bash
cd /opt/erban/event_detection

docker run -d \
  --name event-detection \
  --network host \
  --env-file .env \
  --device /dev/video0:/dev/video0 \
  -v "$PWD/logs:/app/logs" \
  -v "$PWD/result:/app/result" \
  -v "$PWD/certs:/app/certs" \
  guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:20260904
```

`docker run -d` 会创建名为 `event-detection` 的容器，并立即在后台启动。

查看状态和日志：

```bash
docker ps --filter name=event-detection
docker logs -f event-detection
```

## 配置大屏一键启停

事件检测容器创建成功后，大屏不需要重新执行 `docker run`，而是通过固定容器名执行：

```bash
docker start event-detection
docker stop event-detection
```

在 `erban_dashboard/.env` 中配置：

```env
EVENT_DETECTION_BACKEND=docker
EVENT_DETECTION_CONTAINER=event-detection
EVENT_DETECTION_DIR=/opt/erban/event_detection
EVENT_DETECTION_IMAGE=guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:20260904
```

修改大屏自己的 `.env` 后，需要重启大屏服务，使大屏重新读取配置。

大屏当前行为：

- 容器正在运行：接管并显示运行状态。
- 容器存在但已经停止：执行 `docker start event-detection`。
- 点击停止：执行 `docker stop event-detection`。
- 大屏不会在每次启动时重新创建已有容器，因此不会自动刷新事件检测 `.env`。

## `.env` 配置

所有运行配置均通过事件检测目录中的 `.env` 注入。以下列出主要配置，真实 API Key 应单独安全保存。

### 主视觉模型

```env
MAIN_API_BASE=http://127.0.0.1:7060/v1
MAIN_MODEL=your-vlm-model
MODEL_API_KEY=your-api-key

MAIN_DISABLE_THINKING=true
MAIN_MAX_TOKENS=256
MAIN_TEMPERATURE=0.2
MAIN_TOP_P=0.9
```

容器使用 `--network host`，因此 `.env` 中的 `127.0.0.1` 指向宿主机网络。模型服务运行在同一台机器时，可以直接填写其宿主机监听端口。

### 摘要模型与记忆

```env
ENABLE_SUMMARIZER=false
SUMMARIZER_API_BASE=http://127.0.0.1:8065/v1
SUMMARIZER_MODEL=your-summary-model
SUMMARY_DISABLE_THINKING=true

CHUNK=10
ASYNC_SUMMARY_LEAD_FRAMES=5
COMPRESS_EVERY_N_CHUNKS=10

MID_TERM_MAX_TOKENS=256
MID_TERM_TARGET_TOKEN_COUNT=128
LONG_TERM_MAX_TOKENS=256
LONG_TERM_TARGET_TOKEN_COUNT=128
```

主模型和摘要模型可以使用同一个服务，但并发推理可能竞争计算资源。实时性优先时，建议关闭摘要，或者使用独立、轻量的摘要模型。

`ASYNC_SUMMARY_LEAD_FRAMES` 必须满足：

```text
0 <= ASYNC_SUMMARY_LEAD_FRAMES < CHUNK
```

### 图像采集

通用采集参数：

```env
CAPTURE_INTERVAL=0.5
CAPTURE_BUFFER_FRAMES=2
FRAME_SECONDS=0.5
```

- `CAPTURE_INTERVAL`：采集线程写入新帧的目标间隔。
- `CAPTURE_BUFFER_FRAMES`：缓冲区保留的最新帧数量，满时丢弃旧帧。
- `FRAME_SECONDS`：模型上下文中相邻帧的相对时间间隔。

本地摄像头：

```env
VIDEO_SOURCE=camera
CAMERA_ID=0
```

确认设备存在：

```bash
ls -l /dev/video*
```

Foxglove/ROS2：

```env
VIDEO_SOURCE=foxglove
FOXGLOVE_BRIDGE_URL=ws://192.168.217.100:8768
FOXGLOVE_TOPIC=/zj_humanoid/sensor/realsense_head/color/image_raw/compressed
```

ROS1 rosbridge：

```env
VIDEO_SOURCE=rosbridge
ROSBRIDGE_URL=ws://192.168.217.100:9090
FOXGLOVE_TOPIC=/zj_humanoid/sensor/realsense_head/color/image_raw/compressed
```

Zenoh：

```env
VIDEO_SOURCE=zenoh
ZENOH_TOPIC=camera/annotated
ZENOH_URL=tcp/192.168.1.100:7450
ZENOH_CLIENT_FPS=2
```

`ZENOH_CLIENT_FPS=2` 表示事件检测最多以 2 FPS 取用最新图像。返回图像为 RGB 顺序，使用 OpenCV 显示时需要转换为 BGR。

### 大屏和 Agent 推送

```env
SYSTEM_EVENT_URL=http://127.0.0.1:8771/system_event
AGENT_WEBHOOK_URL=http://127.0.0.1:8769/event

DASHBOARD_FRAME_URL=
DASHBOARD_FRAME_INTERVAL=0.1
```

- `SYSTEM_EVENT_URL`：检测事件的大屏接收地址。
- `AGENT_WEBHOOK_URL`：外部 Agent 的事件 Webhook。
- `DASHBOARD_FRAME_URL`：留空时不由事件检测服务向大屏上传画面。
- `DASHBOARD_FRAME_URL=http://127.0.0.1:8771/frame` 时，按 `DASHBOARD_FRAME_INTERVAL` 上传最新帧。

事件二次确认：

```env
EVENT_CONFIRM_ENABLED=true
EVENT_CONFIRM_SECONDS=
```

- `EVENT_CONFIRM_ENABLED`：是否对可确认事件（捂胸/捂头/捂腹/揉肩/跌倒，见 `prompt.CONFIRMABLE_EVENTS`）启用二次确认。
- `EVENT_CONFIRM_SECONDS`：每事件确认延迟（秒）覆盖，JSON 格式，未列出的用默认 2.0 秒。例：`EVENT_CONFIRM_SECONDS={"chest_pain":1.0,"fall":3.0}`。

Langfuse 事件回看：

```env
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
LANGFUSE_BASE_URL=https://47.112.198.26:3000
OTEL_EXPORTER_OTLP_CERTIFICATE=certs/langfuse-erban-cert.pem
LANGFUSE_CHUNK_RECORD_ENABLED=true
LANGFUSE_MEDIA_UPLOAD_ENABLED=false
```

- Langfuse 入口为 nginx 自签 HTTPS（3000 端口），`LANGFUSE_BASE_URL` 必须使用 `https://`，且 `OTEL_EXPORTER_OTLP_CERTIFICATE` 指向 `certs/langfuse-erban-cert.pem`（该证书已随部署目录提供；容器运行需把 `certs/` 挂载进 `/app/certs`）。地址或证书不对时 trace 上报会静默失败（SDK 仅记录 "Failed to export span batch code: 400"）。完整的 HTTP→HTTPS 迁移步骤见 `docs/LANGFUSE_HTTPS_MIGRATION.md`。
- `LANGFUSE_CHUNK_RECORD_ENABLED=false` 时完全不记录 trace。
- 推送过事件的 chunk 会记录一条 Langfuse trace：包含每帧图片（base64 内联）、触发事件、system prompt 及每个 turn 的模型输入输出，用于在 Langfuse UI 回看事件现场、分析误触/漏报。
- `LANGFUSE_MEDIA_UPLOAD_ENABLED=false` 时图片以 base64 内联随 trace 上传，不依赖外部媒体服务。

大屏可以独立采集图像，因此不需要画面上传时建议保持 `DASHBOARD_FRAME_URL` 为空。

## 修改 `.env` 后如何生效

Docker 只在创建容器时读取 `--env-file .env`。以下命令不会重新读取 `.env`：

```bash
docker start event-detection
docker restart event-detection
```

修改 `/opt/erban/event_detection/.env` 后，必须删除旧容器并重新创建：

```bash
cd /opt/erban/event_detection

docker stop event-detection 2>/dev/null || true
docker rm event-detection

docker run -d \
  --name event-detection \
  --network host \
  --env-file .env \
  -v "$PWD/logs:/app/logs" \
  -v "$PWD/result:/app/result" \
  -v "$PWD/certs:/app/certs" \
  guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:20260904
```

如果使用本机摄像头，不要遗漏：

```text
--device /dev/video0:/dev/video0
```

重建容器不会删除宿主机上的 `logs/` 和 `result/`，因为这两个目录通过 volume 挂载保存在容器外部。

## 为什么不能直接重复执行 `docker run`

第一次执行：

```bash
docker run -d --name event-detection ...
```

Docker 会创建并启动容器。以后即使执行了 `docker stop event-detection`，容器仍然存在，只是状态变为 stopped/exited。此时再次执行相同的 `docker run --name event-detection` 会发生名称冲突。

停止后只是重新运行原容器，应使用：

```bash
docker start event-detection
```

需要加载新 `.env` 或更换镜像时，应先执行：

```bash
docker rm -f event-detection
```

然后重新执行完整的 `docker run`。

## 更新事件检测镜像

假设发布了新版本 tag：

```text
guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:NEW_TAG
```

更新流程：

```bash
docker pull \
  guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:NEW_TAG

docker rm -f event-detection

cd /opt/erban/event_detection
docker run -d \
  --name event-detection \
  --network host \
  --env-file .env \
  -v "$PWD/logs:/app/logs" \
  -v "$PWD/result:/app/result" \
  -v "$PWD/certs:/app/certs" \
  guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:NEW_TAG
```

确认新版本正常后，可以删除不再使用的旧版本镜像：

```bash
docker image ls | grep event-detection
docker image rm <旧镜像完整名称:旧TAG>
```

## 镜像发布到阿里云

镜像只从**实际生产运行的机器（如机器人）**发布，不在 amd64 开发机推送：开发机构建出的镜像架构与目标设备不一致（开发机 amd64，机器人多为 arm64），拉到新机器人上无法运行。新机器不构建、不编译，只 `docker pull` 现成镜像。

### 重复推送会不会覆盖

| 情况 | 行为 |
|---|---|
| 推送**同一个** `tag`（例如再次推送 `20260904` 或 `latest`） | **会覆盖**：`tag` 重新指向新镜像，旧镜像变为悬空镜像，仓库垃圾回收后被删除 |
| 推送**不同**的 `tag` | **共存**：多个版本各自可拉取 |
| 想保留旧版本用于回滚 | 推新日期 tag（如 `20260904`），不要动旧 tag（如 `20260831`） |
| 想更新当前生产版本 | 覆盖正在使用的 tag（`latest` 或日期 tag） |

### 在机器人上发布新版本

```bash
# 1. 确认机器人上运行中的容器用的是哪个镜像
docker ps --filter name=event-detection
docker images | grep -E 'event|detect'

# 2. 确认架构（arm64 机器人不要推 amd64 镜像）
docker image inspect event-detection:latest --format 'arch={{.Architecture}}'

# 3. 登录阿里云镜像仓库（首次需要）
docker login guopeilin-registry.cn-hangzhou.cr.aliyuncs.com

# 4. 打标签并推送：新日期 tag（保留旧版本，可回滚）
docker tag event-detection:latest \
  guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:20260904
docker push \
  guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:20260904

# 5. 可选：同步更新 latest，始终指向最新
docker tag event-detection:latest \
  guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:latest
docker push \
  guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:latest

# 6. 验证（任意机器均可执行）
docker manifest inspect \
  guopeilin-registry.cn-hangzhou.cr.aliyuncs.com/erban_agent/event-detection:20260904
```

镜像名不叫 `event-detection:latest` 时，把命令中的镜像名换成第 1 步查到的实际名称。

发布完成后，把本文档中所有引用旧 tag 的 `docker pull` / `docker run` 命令更新为新 tag。只改 `.env` 不需要重新发布镜像，见「修改 `.env` 后如何生效」。

## 常用 Docker 命令

```bash
# 查看运行中的容器
docker ps --filter name=event-detection

# 查看包括已停止容器在内的状态
docker ps -a --filter name=event-detection

# 启动和停止已有容器
docker start event-detection
docker stop event-detection

# 查看日志
docker logs -f event-detection
docker logs --tail 200 event-detection

# 查看容器内已固定的环境变量
docker inspect event-detection --format '{{range .Config.Env}}{{println .}}{{end}}'

# 删除容器；需要加载新 .env 时使用
docker rm -f event-detection
```

注意：查看容器环境变量可能输出 API Key，请勿把结果发送到公开日志或聊天中。

## 本地源码开发

生产机器直接使用阿里云镜像即可。需要修改和调试 Python 代码时，再使用源码方式。

环境要求：

- Python 3.10 或更高版本
- [uv](https://docs.astral.sh/uv/)
- 可访问的 OpenAI 兼容视觉模型服务

安装依赖并启动：

```bash
uv sync
uv run python main.py
```

可选参数：

```bash
uv run python main.py --display
uv run python main.py --show-silence
```

运行测试：

```bash
uv run python -m unittest discover -s tests -v
```

改动 Python 源码后，需要重新构建并发布镜像，生产机器再拉取新 tag。只修改 `.env` 不需要构建或重新拉取镜像。

### 本地构建镜像

```bash
docker build --network host -t event-detection:local .
```

本地镜像可以使用与生产相同的运行参数：

```bash
docker run -d \
  --name event-detection \
  --network host \
  --env-file .env \
  -v "$PWD/logs:/app/logs" \
  -v "$PWD/result:/app/result" \
  -v "$PWD/certs:/app/certs" \
  event-detection:local
```

仓库中的 `docker-compose.yml` 和 `deploy/docker-deploy.sh` 主要用于本地构建、开发部署或创建本地镜像容器。新机器使用阿里云镜像时，以本文前面的 `docker pull` 和 `docker run` 流程为准。

## 常见问题

### `docker run` 提示容器名称冲突

典型错误：

```text
Conflict. The container name "/event-detection" is already in use.
```

如果只是想重新启动已有容器：

```bash
docker start event-detection
```

如果修改了 `.env` 或需要更换镜像：

```bash
docker rm -f event-detection
```

然后重新执行完整的 `docker run`。

### 大屏无法启动或停止容器

依次确认：

1. 大屏 `.env` 中设置了 `EVENT_DETECTION_BACKEND=docker`。
2. 容器名称确实是 `event-detection`。
3. 大屏服务运行用户可以执行 `docker ps` 和 `docker start event-detection`。
4. Docker daemon 正在运行。
5. 修改大屏 `.env` 后已经重启大屏服务。

### 修改事件检测 `.env` 后配置没有变化

`docker restart` 不会重新加载 `.env`。删除旧容器，然后使用最新 `.env` 重新执行完整的 `docker run`。

### 容器启动后立即退出

```bash
docker ps -a --filter name=event-detection
docker logs --tail 200 event-detection
docker inspect event-detection --format '{{.State.ExitCode}}'
```

常见原因包括 `.env` 缺少模型配置、模型地址不可访问、视频源配置错误，以及本地摄像头没有映射进容器。

### 无法打开 `/dev/video0`

```bash
ls -l /dev/video*
```

使用本地摄像头时，`docker run` 必须包含：

```text
--device /dev/video0:/dev/video0
```

没有本地摄像头时，将 `VIDEO_SOURCE` 改为 `foxglove`、`rosbridge` 或 `zenoh`，并重建容器。

### Zenoh 无法收到图像

确认：

1. `ZENOH_URL` 的 IP 和端口可以从新机器访问。
2. `ZENOH_TOPIC` 与发布端完全一致。
3. 发布端正在运行并持续发布图像。
4. 防火墙允许对应 TCP 端口。

### Foxglove 或 rosbridge 连接失败

确认 WebSocket 地址、端口和图像话题正确，并确认服务端不是只监听其他网卡地址。容器使用 host 网络，不需要额外做 Docker 端口映射。

### 大屏能打开但没有画面

- `DASHBOARD_FRAME_URL` 非空时，检查事件检测上传日志和大屏接收接口。
- `DASHBOARD_FRAME_URL` 为空时，检查大屏自身的视频源配置和采集日志。
- 事件推送和大屏画面采集是两条独立链路，其中一条正常不代表另一条一定正常。

### 摘要导致检测延迟升高

- 设置 `SUMMARY_DISABLE_THINKING=true`。
- 降低中期和长期摘要的最大 token 数。
- 适当增大 `ASYNC_SUMMARY_LEAD_FRAMES`，但必须小于 `CHUNK`。
- 增大 `COMPRESS_EVERY_N_CHUNKS`，降低长期压缩频率。
- 主检测与摘要共用模型服务时，检查服务端资源竞争。

## Agent 临时动作监测 Server

镜像中还包含独立的 `agent_monitor_server`。它与原固定事件检测使用同一份
`.env`，但运行在独立的 `agent-monitor-server` 容器中。Server 空闲时不连接
视频源；收到任务后才开始采集和推理，任务到期后自动释放视频源。

新增配置（均为可选默认值）：

```env
MONITOR_SERVER_HOST=127.0.0.1
MONITOR_SERVER_PORT=8781
MONITOR_AUTH_TOKEN=
MONITOR_MIN_TIMEOUT_SECONDS=1
MONITOR_MAX_TIMEOUT_SECONDS=3600
MONITOR_MAX_PROMPT_LENGTH=16000
MONITOR_PUSH_TIMEOUT_SECONDS=3
MONITOR_EXPAND_TIMEOUT_SECONDS=30
MONITOR_EXPAND_MAX_TOKENS=1024
MONITOR_FRAME_INTERVAL=0.5
MONITOR_MAX_TOKENS=256
MONITOR_TEMPERATURE=0.2
MONITOR_MAX_CONTEXT_TURNS=20
```

若 Agent 位于其他机器，将 `MONITOR_SERVER_HOST` 设置为 `0.0.0.0`，并配置
足够长的 `MONITOR_AUTH_TOKEN`。模型、视频源、`AGENT_WEBHOOK_URL` 和
`SYSTEM_EVENT_URL` 继续复用现有配置。可选的 `MONITOR_MAIN_API_BASE` 和
`MONITOR_MAIN_MODEL` 可以覆盖主模型地址和模型名。

创建或替换任务：

```bash
curl -X POST http://127.0.0.1:8781/monitor \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer your-token' \
  -d '{
    "event_name": "raise_left_hand",
    "prompt": "监测画面中的人是否举起左手",
    "timeout_seconds": 60
  }'
```

`timeout_seconds` 是任务监测时长。有效新任务会替换旧任务并清空旧上下文；
每一轮模型输出的 `event` 与 `event_name` 相同都会同时推送 Agent 和大屏，
不做去重。Agent 只提供简短任务描述（`prompt` 或 `instruction` 字段），
Server 收到请求后立即使用主模型仿照内置模板结构扩写为完整监测提示词，
扩写完成后按 `timeout_seconds` 启动监测；扩写失败或输出不合法时自动回退
到内置确定性模板，任务仍可正常启动。

运维接口：

```text
GET    /health
GET    /monitor/current
DELETE /monitor
```

两个容器可独立管理：

```bash
docker start agent-monitor-server
docker logs -f agent-monitor-server
docker stop agent-monitor-server
```

使用 `VIDEO_SOURCE=camera` 时，多数 USB 摄像头不支持两个容器同时打开；需要
同时运行两种检测时推荐使用 Zenoh、Foxglove 或 rosbridge 视频源。

## 主要文件

| 文件 | 作用 |
|---|---|
| `main.py` | 程序入口，启动适配器线程和图像采集循环。 |
| `webinfer/live_adapter.py` | 主视觉模型、会话、chunk、摘要、记忆和事件推送。 |
| `webinfer/memory_summarizer.py` | 中长期摘要模型客户端。 |
| `prompt.py` | 事件检测和人物行为记忆提示词。 |
| `foxglove_client.py` | Foxglove WebSocket 和 ROS2 图像解码。 |
| `zenoh_client.py` | Zenoh 图像订阅。 |
| `.env` | 本机运行配置，不应提交到 Git。 |
| `Dockerfile` | 本地构建事件检测镜像。 |
| `docker-compose.yml` | 本地 Compose 编排。 |
| `deploy/docker-deploy.sh` | 本地构建并创建容器的辅助脚本。 |
| `agent_monitor_server/` | Agent 动态提示词、限时动作监测 HTTP Server。 |

## 安全注意事项

- 不要把 `.env`、API Key、阿里云登录密码或访问令牌提交到 Git。
- 不要在公开日志中输出 `docker inspect` 得到的完整环境变量。
- 生产环境使用明确的镜像 tag，例如 `20260904`，避免仅依赖可能变化的 `latest`。
- 更新镜像前保留当前可用 tag，便于出现问题时回滚。
- `logs/` 和 `result/` 可能包含业务数据，应按部署环境设置访问权限和保留策略。
