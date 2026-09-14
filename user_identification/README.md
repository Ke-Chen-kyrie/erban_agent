# 用户身份识别服务

人脸 + 声纹生物特征认证服务，支持可插拔引擎 — 人脸识别支持阿里云 FaceBody / InsightFace，声纹识别支持讯飞云 / CAM++ / ERes2Net / ECAPA。

## 功能特性

- **人脸识别** — 1:1 验证、1:N 搜索、多人脸检测（含关键点）
- **声纹识别** — 1:N 声纹搜索，可配置评分阈值
- **可插拔引擎** — 通过 `.env` 切换云端 API 和本地 ONNX 模型
- **GPU 加速** — 自动检测 CUDA，支持 InsightFace 和边缘声纹引擎
- **双模式部署** — 本地 `uv` 部署或 Docker Compose 部署
- **SDK 客户端** — 可 pip 安装的 Python SDK（`IdentificationClient/`）

## API 接口

所有接口返回 JSON，文件上传使用 `multipart/form-data`。

### 用户管理

| 方法 | 路径 | 说明 |
|--------|------|-------------|
| `POST` | `/api/user/register` | 注册用户（人脸图片 + 声纹音频） |
| `GET` | `/api/user/list` | 列出所有用户 |
| `GET` | `/api/user/{user_id}` | 查询用户信息 |
| `GET` | `/api/user/{user_id}/face` | 获取用户人脸图片（base64） |
| `DELETE` | `/api/user/{user_id}` | 删除用户及所有生物特征数据 |

#### POST `/api/user/register`

| 参数 | 类型 | 必填 | 说明 |
|-----------|------|----------|-------------|
| `user_id` | form 字符串 | 是 | 用户唯一标识 |
| `name` | form 字符串 | 是 | 显示名称 |
| `role` | form 字符串 | 否 | 角色（默认 `""`） |
| `description` | form 字符串 | 否 | 描述（默认 `""`） |
| `face_image` | 文件 | 是 | 人脸图片（JPEG / PNG / BMP） |
| `voice_audio` | 文件 | 是 | 声纹音频文件 |

返回: `{ user_id, name, role, description, face_id, message }`

### 人脸识别

| 方法 | 路径 | 说明 |
|--------|------|-------------|
| `POST` | `/api/face/search` | 1:N 人脸搜索 |
| `POST` | `/api/face/verify` | 1:1 人脸验证 |
| `POST` | `/api/face/detect` | 多人脸检测与身份识别 |
| `POST` | `/api/face/analyze` | 人脸原始推理结果（仅 InsightFace） |

#### POST `/api/face/search`

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|-----------|------|----------|---------|-------------|
| `image` | 文件 | 是 | — | 人脸图片 |
| `top_k` | form 整数 | 否 | `5` | 最大返回数量 |

返回: `{ results: [{ entity_id, face_id, score, confidence, name }], request_id }`

#### POST `/api/face/verify`

| 参数 | 类型 | 必填 | 说明 |
|-----------|------|----------|-------------|
| `user_id` | form 字符串 | 是 | 待验证的用户 ID |
| `image` | 文件 | 是 | 人脸图片 |

返回: `{ user_id, entity_id, score, match, threshold, request_id }`

#### POST `/api/face/detect`

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|-----------|------|----------|---------|-------------|
| `image` | 文件 | 是 | — | 包含人脸的图片 |
| `max_face_num` | form 整数 | 否 | `10` | 最大检测人脸数 |

返回: `{ faces: [{ user_id, name, score, matched, location, mouth }], total_faces, matched_count, unknown_count }`

#### POST `/api/face/analyze`

> 仅 `FACE_ENGINE=insightface` 时可用，返回模型原始推理结果，不查库、不匹配。

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|-----------|------|----------|---------|-------------|
| `image` | 文件 | 是 | — | 人脸图片 |
| `max_face_num` | form 整数 | 否 | `10` | 最大检测人脸数 |

返回: `{ faces: [{ bbox, det_score, kps, pose }], request_id }`

| 字段 | 类型 | 说明 |
|-----------|------|-------------|
| `bbox` | `[float, float, float, float]` | 边界框 `[x1, y1, x2, y2]` |
| `det_score` | `float` | SCRFD 检测置信度 0~1 |
| `kps` | `[[float,float],...]` \| `null` | 5 点关键点（双眼、鼻尖、嘴角） |
| `pose` | `[float,float,float]` \| `null` | 人脸姿态角 `[yaw, pitch, roll]` |

### 声纹识别

| 方法 | 路径 | 说明 |
|--------|------|-------------|
| `POST` | `/api/voice/search` | 1:N 声纹搜索 |

#### POST `/api/voice/search`

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|-----------|------|----------|---------|-------------|
| `audio` | 文件 | 是 | — | 声纹音频 |
| `top_k` | form 整数 | 否 | `1` | 最大返回数量 |

返回: `{ results: [{ score, user_id, name }], score_list }`

### 系统

| 方法 | 路径 | 说明 |
|--------|------|-------------|
| `GET` | `/api/health` | 健康检查 |
| `POST` | `/api/admin/sync` | 清理云端孤儿数据 |

## 部署

### 本地部署

**前置条件**：Python >= 3.10，[uv](https://github.com/astral-sh/uv)

```bash
# 1. 配置
cp .env.example .env
vim .env  # 填入凭证

# 2. 一键部署（自动检测 CUDA）
bash deploy/docker-deploy.sh --network-host
```

脚本自动完成：创建 venv → 安装依赖 → 检测 CUDA → 安装对应 onnxruntime → 启动服务。

**手动安装**：

```bash
uv venv --python 3.10
source .venv/bin/activate
uv pip install -r requirements.txt
bash deploy/deploy.sh  # 或: python main.py
```

**安装测试客户端依赖**（需要摄像头和麦克风）：

```bash
INSTALL_CLIENT=1 bash deploy/deploy.sh
```

### Docker 部署

**前置条件**：Docker + Docker Compose

#### CPU 模式（所有平台通用）

```bash
cp .env.example .env
vim .env
bash deploy/docker-deploy.sh
```

#### GPU 模式

```bash
cp .env.example .env
vim .env
bash deploy/docker-deploy.sh --gpu
```

`--gpu` 参数使用 `docker-compose.gpu.yml` 叠加文件，启用 `runtime: nvidia` 并设置 `ONNXRUNTIME_MODE=gpu`。

| 平台 | onnxruntime 安装来源 |
|----------|-------------------|
| x86_64 | `pip install onnxruntime-gpu` |
| aarch64（Jetson Orin） | 项目根目录 `.whl` 文件 |

**Jetson Orin GPU 部署前提**：

1. 确保已安装 `nvidia-container-toolkit`
2. 将 `onnxruntime_gpu-*-cp310-cp310-linux_aarch64.whl` 放在项目根目录（从 [onnxruntime releases](https://github.com/microsoft/onnxruntime/releases) 下载）

**Docker 常用命令**：

```bash
# CPU
docker compose up -d

# GPU
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d

# 常用操作
docker compose logs -f        # 查看日志
docker compose restart        # 重启
docker compose down           # 停止并删除容器
```

### GPU 启用说明

服务启动时自动检测 CUDA。在 `.env` 中配置：

```bash
# 人脸引擎 — InsightFace GPU
FACE_ENGINE=insightface
FACE_DEVICE=auto          # 自动检测 CUDA；也可设为 "cuda" 或 "cpu"

# 声纹引擎 — 边缘模型 GPU
VOICEPRINT_ENGINE=ecapa    # 或 campp / eres2net
VOICEPRINT_DEVICE=auto     # 自动检测 CUDA；也可设为 "cuda" 或 "cpu"
```

- `auto` — 自动检测，CUDA 不可用时自动回退 CPU
- `cuda` — 强制使用 CUDA，不可用时警告并回退 CPU
- `cpu` — 始终使用 CPU

`deploy.sh` 脚本也会通过 `nvidia-smi` 或 `/usr/local/cuda` 检测 CUDA，自动安装对应的 `onnxruntime` 版本。

## 配置说明

复制 `.env.example` 为 `.env` 并编辑：

| 变量 | 说明 | 默认值 |
|----------|-------------|---------|
| `IFLYTEK_APP_ID` | 讯飞声纹 App ID | — |
| `IFLYTEK_API_KEY` | 讯飞 API Key | — |
| `IFLYTEK_API_SECRET` | 讯飞 API Secret | — |
| `ALIBABA_AK_ID` | 阿里云 AccessKey ID | — |
| `ALIBABA_AK_SECRET` | 阿里云 AccessKey Secret | — |
| `FACE_ENGINE` | 人脸引擎：`alibaba` / `insightface` | `alibaba` |
| `FACE_DEVICE` | InsightFace 设备：`auto` / `cuda` / `cpu` | `auto` |
| `FACE_DB_NAME` | 云端人脸库名称（租户隔离） | `default` |
| `FACE_MIN_SCORE` | 人脸搜索最低评分 0~1 | `0.5` |
| `FACE_VERIFY_THRESHOLD` | 1:1 验证阈值 | `0.5` |
| `VOICEPRINT_ENGINE` | 声纹引擎：`xunfei` / `campp` / `eres2net` / `ecapa` | `xunfei` |
| `VOICEPRINT_DEVICE` | 边缘声纹设备：`auto` / `cuda` / `cpu` | `cpu` |
| `VOICEPRINT_MODEL_PATH` | 边缘引擎 ONNX 模型路径 | — |
| `VOICE_GROUP_ID` | 云端声纹组 ID（租户隔离） | `default` |
| `VOICE_MIN_SCORE` | 声纹搜索最低评分 0~1 | `0.5` |

## 引擎选择

### 人脸引擎

| 引擎 | 类型 | 设备 | 说明 |
|--------|------|--------|-------|
| `alibaba` | 云端 API | — | 需要阿里云凭证 |
| `insightface` | 本地 ONNX | CPU / GPU | SCRFD + ArcFace（buffalo_l），自动下载模型 |

```bash
# 阿里云
FACE_ENGINE=alibaba

# InsightFace GPU
FACE_ENGINE=insightface
FACE_DEVICE=cuda
```

### 声纹引擎

| 引擎 | 类型 | 设备 | 准确率 | 模型路径 |
|--------|------|--------|----------|-------|
| `xunfei` | 云端 API | — | — | — |
| `campp` | CAM++ | CPU / GPU | 92.3% | `voiceprint/campp/models/campplus_cn_common.onnx` |
| `eres2net` | ERes2Net | CPU / GPU | 92.8% | `voiceprint/eres2net/models/eres2net.onnx` |
| `ecapa` | ECAPA-TDNN | CPU / GPU | 90.5% | `voiceprint/ecapa/models/ecapa_tdnn.onnx` |

```bash
# 讯飞云
VOICEPRINT_ENGINE=xunfei

# ECAPA GPU
VOICEPRINT_ENGINE=ecapa
VOICEPRINT_MODEL_PATH=voiceprint/ecapa/models/ecapa_tdnn.onnx
VOICEPRINT_DEVICE=cuda
```

修改 `.env` 后需重启服务。

## 测试 CLI

```bash
python testIdentificationClient.py health          # 健康检查
python testIdentificationClient.py register u001 张三 admin  # 注册用户
python testIdentificationClient.py face            # 1:N 人脸搜索
python testIdentificationClient.py voice           # 1:N 声纹搜索
python testIdentificationClient.py verify u001     # 1:1 人脸验证
python testIdentificationClient.py getface u001    # 获取用户人脸图片
python testIdentificationClient.py info u001       # 查询用户信息
python testIdentificationClient.py list_user       # 列出所有用户
python testIdentificationClient.py detect          # 多人脸检测
python testIdentificationClient.py sync            # 同步清理云端数据
python testIdentificationClient.py delete u001     # 删除用户

# 交互模式
python testIdentificationClient.py
```

## 项目结构

```
user_identification/
├── deploy/
│   ├── deploy.sh              # 本地一键部署脚本
│   └── docker-deploy.sh       # Docker 一键部署脚本
├── main.py                    # FastAPI 入口
├── config.py                  # 凭证 + 引擎配置
├── settings.py                # 路径与端点配置
├── database.py                # SQLite 初始化
├── exceptions.py              # 统一异常类型
├── requirements.txt           # Python 依赖
├── Dockerfile                 # Docker 镜像构建
├── docker-compose.yml         # Docker CPU 配置
├── docker-compose.gpu.yml     # Docker GPU 叠加配置
├── .env.example               # 配置模板
├── routers/
│   ├── user.py                # 注册 / 删除 / 查询
│   ├── face.py                # 人脸搜索与验证
│   └── voice.py               # 声纹搜索
├── services/
│   ├── face_service.py        # 双引擎人脸服务
│   ├── face_engine_insightface.py  # InsightFace 本地引擎
│   ├── voice_service.py       # 可插拔声纹引擎封装
│   └── face_storage.py        # 本地人脸图片存储
├── schemas/                   # Pydantic 响应模型
├── models/user.py             # 数据库操作
├── voiceprint/                # 声纹 SDK 与 ONNX 模型
│   ├── xunfei-cloud/          # 讯飞云 SDK
│   ├── campp/                 # CAM++ SDK + 模型
│   ├── eres2net/              # ERes2Net SDK + 模型
│   ├── ecapa/                 # ECAPA SDK + 模型
│   └── _shared/               # 共享边缘 SDK 层
├── IdentificationClient/      # Python SDK
│   └── client.py
└── testIdentificationClient.py  # CLI 测试工具
```