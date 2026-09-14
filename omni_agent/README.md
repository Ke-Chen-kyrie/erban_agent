# 小伴 — 具身智能康养机器人语音助手

"小伴"是由迩伴智能机器人（深圳）有限公司研发的具身智能老年人护理机器人语音助手。系统通过多模态流水线实现自然语音交互：唤醒词检测 → 语音识别 → 声纹识别 → 视频采集 → 大模型对话 → 原生语音输出，并具备喂饭、喂水、转身等机器人物理操控能力。

---

## 目录

- [系统要求](#系统要求)
- [外部服务依赖](#外部服务依赖)
- [快速开始](#快速开始)
- [项目结构](#项目结构)
- [架构设计](#架构设计)
- [工具系统](#工具系统)
- [音色列表](#音色列表)
- [调试与排错](#调试与排错)
- [部署清单](#部署清单)

---

## 系统要求

| 项目 | 要求                                                            |
|------|---------------------------------------------------------------|
| 操作系统 | Linux（Ubuntu 22.04+ / Deepin 等）                               |
| Python | 3.12                                                          |
| GPU | 可选（唤醒词 + sherpa-onnx 本地 ASR 均支持 CUDA 加速）                      |
| 麦克风 | USB 无线麦克风，设备名含关键词 `"Wireless Mic Rx"`                         |
| 扬声器 | YUNJI 音频输出设备，设备名含关键词 `"YUNJI"`                                |
| 摄像头 | 2 路 Intel RealSense 摄像头（头部 + 胸部），通过 ROS2 + Foxglove Bridge 接入 |
| 机器人本体 | 迩伴机器人                                                         |

## 外部服务依赖

系统依赖 7 个外部服务，分为**机器人侧**（局域网）和**云端**两组。启动前需确保所有服务均已就绪。

### 网络拓扑

```
┌─────────────────────────────────────────────────────────────┐
│                        机器人本体                            │
│  ┌──────────────────────┐  ┌──────────────────────────────┐ │
│  │ Foxglove Bridge      │  │ 机器人控制器                  │ │
│  │ (ROS2 摄像头画面)     │  │ (turn/food/water 物理指令)    │ │
│  └──────────┬───────────┘  └──────────────┬───────────────┘ │
└─────────────┼──────────────────────────────┼─────────────────┘
              │                              │
┌─────────────┼──────────────────────────────┼─────────────────┐
│             │             工控机                             │
│  ┌──────────┴───────────┐  ┌───────────────┴───────────────┐ │
│  │ 声纹+人脸识别服务      │  │ Shell 代理服务器              │ │
│  │ (注册/搜索/验证)       │  │ (CLI工具集: water/food/      │ │
│  │                       │  │  turn/ident/search/          │ │
│  │                       │  │  weather/cur-time)           │ │
│  └──────────────────────┘  └──────────────────────────────┘ │
└─────────────────────────────────────────────────────────────┘
              │
              │          互联网（云端 API）
              ▼
┌─────────────────────────────────────────────────────────────┐
│  ┌────────────────────┐ ┌──────────────┐ ┌───────────────┐ │
│  │ 火山引擎 SeedASR   │ │ 阿里云 DashScope│ │ 阿里云 IQS     │ │
│  │ 实时语音转写         │ │ Qwen-Omni 大模型│ │ 搜索 + 天气    │ │
│  └────────────────────┘ └──────────────┘ └───────────────┘ │
└─────────────────────────────────────────────────────────────┘
```

### 服务清单

| 服务 | 配置变量 | 用途 | 负责方 |
|------|------|------|--------|
| Foxglove WebSocket Bridge | `FOXGLOVE_BRIDGE_URL` | ROS2 摄像头画面订阅（sensor_msgs/CompressedImage） | 机器人/嵌入式 |
| 声纹 + 人脸识别服务 | `IDENTIFICATION_URL` | 用户注册、声纹搜索、人脸验证、用户信息查询 | 识别服务 |
| Shell 代理服务器 | `SHELL_PROXY_URL` | 远程命令执行（接收 `{"cmd": "<command>"}`），分发到 CLI 工具集 | 机器人/嵌入式 |
| 火山引擎 SeedASR 2.0 | `BYTEDANCE_ASR_URL` | 实时语音转文字（WebSocket 流式，二遍识别）。`ASR_PROVIDER=cloud` 时需要 | 火山引擎 |
| 阿里云 DashScope | `QWEN_LLM_CONFIG["base_url"]` | Qwen-Omni 大模型（Omni 模式：文本理解 + 原生语音生成） | 阿里云 |
| 火山引擎 Ark（豆包） | `BYTEDANCE_LLM_CONFIG["base_url"]` | Doubao 大模型（LLM_PROVIDER=doubao 时使用） | 火山引擎 |
| 阿里云 IQS | CLI 工具依赖（search/weather 命令） | 联网搜索 + 天气查询 | 阿里云 |

---

## 快速开始

### 1. 安装 uv

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### 2. 克隆项目并安装系统依赖

```bash
git clone <repo-url> /opt/omni_agent
cd /opt/omni_agent

# 系统依赖（首次部署需 sudo）
sudo apt install -y alsa-utils ffmpeg portaudio19-dev
```

### 3. 创建虚拟环境并安装 Python 依赖

```bash
uv venv --python 3.12
uv pip install -r requirements.txt
```

### 4. 下载模型

**唤醒词模型**（必选）：下载 [sherpa-onnx KWS](https://github.com/k2-fsa/sherpa-onnx/releases) 模型，放置到 `models/wakeup/`：

```
models/wakeup/
├── tokens.txt
├── encoder-epoch-13-avg-2-chunk-8-left-64.onnx
├── decoder-epoch-13-avg-2-chunk-8-left-64.onnx
└── joiner-epoch-13-avg-2-chunk-8-left-64.onnx
```

**sherpa-onnx 本地 ASR 模型**（可选，`ASR_PROVIDER=sherpa_onnx` 时需要）：存放于 `models/sherpa-onnx-streaming-zipformer-bilingual-zh-en/`。

### 5. 配置环境变量

参考 `config.py` 中的配置项创建 `.env` 文件：

```bash
vim .env
```

必填项：

| 变量 | 说明 |
|------|------|
| `BYTEDANCE_API_KEY` | 火山引擎 ASR API Key |
| `BYTEDANCE_APP_KEY` | 火山引擎 App Key |
| `BYTEDANCE_ACCESS_KEY` | 火山引擎 Access Key |
| `DASHSCOPE_API_KEY` | 阿里云 DashScope API Key（千问 Omni + 摘要模型共用） |
| `ARK_API_KEY` | 火山引擎 Ark API Key（格式 `ark-` 开头，豆包模型使用） |
| `LLM_PROVIDER` | `"doubao"` 或 `"qwen_omni"`，默认 `"doubao"` |
| `AUDIO_SOURCES` | `"tts"` 或 `"omni"`，默认 `"tts"` |
| `ASR_PROVIDER` | `"cloud"`（ByteDance SeedASR）或 `"sherpa_onnx"`（sherpa-onnx 本地），默认 `"cloud"` |

全部配置项见 `config.py`。

### 6. 验证环境

```bash
# 检查音频设备
uv run python -c "import pyaudio; p=pyaudio.PyAudio(); [print(p.get_device_info_by_index(i)['name']) for i in range(p.get_device_count())]"

# 检查扬声器设备
uv run python -c "import sounddevice; print(sounddevice.query_devices())"

# 检查网络连通性（以下为示例，请替换为实际配置的地址）
curl -s "$IDENTIFICATION_URL/api/health"
curl -s -X POST "$SHELL_PROXY_URL" -d '{"cmd":"cur-time"}'
```

### 7. 启动

```bash
uv run python main.py
```

启动流程：
1. 加载 sherpa-onnx 唤醒模型并执行预热推理（约 5-10 秒）
2. 从识别服务拉取已注册用户列表
3. 开始监听麦克风，等待唤醒词 `"小伴小伴"`

`Ctrl+C` 退出。

### 一键部署

```bash
bash deploy/deploy.sh
```

完成步骤：检查系统依赖 → 安装 uv → 创建 venv + 安装依赖 → 配置 .env → 前台启动应用。

---

## 项目结构

```
omni_agent/
│
├── main.py                          # 入口：VoiceAssistantApp 主循环（唤醒→应答→多轮对话）
├── config.py                        # 所有配置项集中管理
├── requirements.txt                 # Python 依赖清单
│
├── audio_capture.py                 # 麦克风采集（PyAudio, 设备原生采样率→16kHz 输出）
├── wakeup_detector.py               # 唤醒词检测（sherpa-onnx KWS, CPU/CUDA）
├── bytedance_asr.py                 # 火山引擎 SeedASR 2.0（WebSocket 长连接，二遍识别，VAD 分句）
├── sherpa_onnx_asr.py               # sherpa-onnx 本地流式 ASR（zipformer 中英双语）
├── base_asr.py                      # ASR 基类（会话管理、音频缓冲、WAV 导出）
├── bytedance_tts.py                 # 火山引擎 Seed-TTS 2.0（WebSocket 流式 TTS）
├── tts_protocols.py                 # TTS 二进制协议编解码
├── audio_output.py                  # 扬声器播放（sounddevice OutputStream）
├── video_capture.py                 # 视频录制（Foxglove Bridge 订阅 ROS2 摄像头，AVI 编码）
├── voice_interrupt_detector.py      # 语音打断检测（能量 VAD + 滑动窗口投票）
├── pipeline.py                      # 视频帧差压缩 + 编码 + PerfMetrics 性能计时
├── foxglove_client.py               # Foxglove WebSocket 客户端（发现、订阅、反序列化）
│
├── agent_client.py                  # ChatSession 多轮对话客户端（薄封装层）
│
├── agent/
│   ├── agent.py                     # LangGraph Agent 图定义（4 节点：memory_compress + omni + tools + inject_camera）
│   ├── tools.py                     # LangChain 工具定义（8 个工具，含 execute_shell 远程代理）
│   ├── utils.py                     # 辅助函数 + 跨模块共享状态（声纹、TTS fallback、睡眠信号等）
│   ├── router.py                    # 多模态意图路由（关键词+小模型，决定是否发送视频/音频给 LLM）
│   ├── skill_manager.py             # SKILL.md 技能文件加载器
│   ├── prompt.py                    # 所有提示词模板（系统提示词、路由器、摘要）
│   ├── gesture_detector.py          # MediaPipe 手势检测（点头/摇头/挥手/举手）
│   └── skills/
│       ├── feed_food/SKILL.md       # 喂饭技能完整操作指南
│       └── feed_water/SKILL.md      # 喂水技能完整操作指南
│
├── IdentificationClient/
│   └── client.py                    # 声纹+人脸识别服务 HTTP SDK
│
├── percept_environment.py           # 感知环境状态机（ASR + VideoRecorder 生命周期管理）
├── system_event_server.py           # 系统事件 HTTP 服务器（接收第三方传感器事件）
├── notice.py                        # 系统事件提交 CLI 工具（独立脚本）
├── logging_config.py                # 日志配置
├── deploy/
│   └── deploy.sh                    # 一键部署脚本
├── models/                          # 模型文件目录
│   ├── wakeup/                      # sherpa-onnx 唤醒模型
│   └── sherpa-onnx-streaming-.../   # sherpa-onnx 流式 ASR 模型（中英双语）
├── CLAUDE.md                        # Claude Code AI 编程助手指南
└── README.md                        # 本文件
```

---

## 架构设计

### 整体数据流

```
                              ┌───────────────────────┐
                              │     AudioCapture       │
                              │  (PyAudio, daemon线程)  │
                              │  原生采样率 → 16kHz     │
                              └───────────┬───────────┘
                                          │ audio_queue (queue.Queue, maxsize=80)
                                          │ float32 numpy chunks
                                          ▼
              ┌───────────────────────────────────────────────────────┐
              │                  音频分发 (3 个消费者)                  │
              │                                                       │
              │  ┌─────────────────┐  ┌──────────────┐  ┌───────────┐ │
              │  │ WakeupDetector  │  │ ASR (云端/本地)│  │ 打断检测   │ │
              │  │ (sherpa-onnx)   │  │ SeedASR /    │  │ (能量VAD)  │ │
              │  │ "小伴小伴"       │  │ 流式 ASR 转写  │  │ 滑动窗口   │ │
              │  └───────┬─────────┘  └──────┬───────┘  └─────┬─────┘ │
              │          │                   │                │       │
              │          │ 互斥：同一时刻只有一个消费者持有音频    │       │
              └──────────┼───────────────────┼────────────────┼───────┘
                         │                   │                │
                         ▼                   ▼                ▼
                  唤醒回调触发          ASR 识别结果       打断标志置位
                  on_wakeup()         sentence_queue     _interrupted
                         │                   │                │
                         ▼                   │                │
              ┌──────────────────┐            │                │
              │ 声纹识别 (唤醒音频) │            │                │
              │ → user_id/name/role           │                │
              └────────┬─────────┘            │                │
                       │                      │                │
                       ▼                      ▼                │
              ┌────────────────────────────────────────┐      │
              │         唤醒应答                     │      │
              │  "你好，小伴！"（固定文本，跳过路由）  │      │
              │  通过 LangGraph Agent 流式应答       │      │
              └──────────────────┬─────────────────────┘      │
                                 │                            │
                                 ▼                            │
              ┌──────────────────────────────────────────┐    │
              │           多轮对话循环 (asyncio)           │    │
              │                                          │    │
              │  1. ASR 激活 → 等待用户说话               │    │
              │  2. 声纹识别 (每句) → 编码视频            │    │
              │  3. LangGraph Agent 流式推理              │◄───┘
              │  4. AudioPlayer 播放语音                  │  (打断时置位)
              │  5. 排空音频队列 → 回到步骤 1             │
              └──────────────────────────────────────────┘
```

### LangGraph Agent 图结构

```
                         ┌──────────┐
                         │  START   │
                         └────┬─────┘
                              │
                              ▼
              ┌───────────────────────────────┐
              │       memory_compress          │
              │  超过 15 轮未压缩对话时，        │
              │  将旧消息压缩为摘要注入          │
              │  系统提示词（否则跳过）           │
              └───────────────┬───────────────┘
                              │
                              ▼
              ┌───────────────────────────────┐
              │         omni                  │
              │  1. 构建 System Prompt          │
              │  2. 构建多模态消息               │
              │  3. 调用 LLM API               │
              │  4. 输出: text token + audio PCM │
              └───────────────┬───────────────┘
                              │
                         ┌────▼────┐
                         │ 有 tool  │
                         │ _calls? │
                         └────┬────┘
                  ┌───────────┘│
                  │ 否         │ 是
                  ▼            ▼
            ┌─────────┐  ┌──────────┐
            │   END   │  │  tools   │
            └─────────┘  │ (ToolNode)│
                         └────┬─────┘
                              │
                              ▼
              ┌───────────────────────────────┐
              │       inject_camera            │
              │  处理 observe_environment      │
              │  工具结果 → camera_captured_*  │
              └───────────────┬───────────────┘
                              │
                              └──────────► omni
```

### LLM 配置

项目使用统一的 `_get_llm_client(config)` 工厂函数创建客户端，三个配置字典结构一致：

| 配置 | 用途 | 模型 |
|------|------|------|
| `QWEN_LLM_CONFIG` | 千问 Omni 大模型（LLM_PROVIDER=qwen_omni） | `qwen3.5-omni-plus` |
| `BYTEDANCE_LLM_CONFIG` | 豆包大模型（LLM_PROVIDER=doubao） | `doubao-seed-2-0-lite-260428` |
| `SMALL_INTENT_LLM_CONFIG` | 千问小模型（记忆压缩 + 路由器回退） | `qwen3.6-35b-a3b` |

每个配置字典均包含 `api_key`、`base_url` 字段（`_get_llm_client()` 使用这两个字段）。`QWEN_LLM_CONFIG` 和 `BYTEDANCE_LLM_CONFIG` 额外包含 `models` 字典（`large`/`small` 两个模型名），`SMALL_INTENT_LLM_CONFIG` 直接使用 `model` 字段。

### 关键设计决策

#### 1. 历史压缩策略

当未压缩的 HumanMessage 超过 `HISTORY_KEEP_ROUNDS`（默认 15）轮时，使用千问小模型将旧消息压缩为约 500 字的中文滚动摘要，注入系统提示词。最新一轮对话始终保留完整内容。

#### 2. 语音打断检测

利用近场效应：用户嘴离麦克风仅 10-30cm，语音能量远大于扬声器回声。算法：每 80ms 帧计算 RMS 能量 → 超过阈值判为"人声" → 滑动窗口（20 帧）内 ≥4 帧人声即触发打断。

#### 3. 音频队列排空

每次 AI 播报结束后排空麦克风缓冲队列中的残留音频，避免扬声器回声被 ASR 误识别为用户输入。

#### 4. 摄像头媒体隔离

`observe_environment` 工具拍摄的图片/视频/音频走独立状态字段（`camera_captured_images`/`videos`/`audios`），与用户说话时伴随的媒体（`latest_images_base64`/`videos_base64`）严格隔离，避免旧媒体被重复发送。

#### 5. 技能系统（安全机制）

物理操作（喂饭、喂水）必须遵循双重安全机制：
1. 必须以用户明确的口头指令作为唯一执行前提
2. 必须先调用 `get_skill_detail(skill_name)` 加载完整操作指南，严格按指南步骤执行

技能文件位于 `agent/skills/<name>/SKILL.md`，由 `SkillLoader` 自动扫描并注入到 System Prompt。

---

## 工具系统

### 直接工具（LangChain Tool）

| 工具 | 参数 | 功能 |
|------|------|------|
| `observe_environment` | `duration: int = 0` | 拍照（duration=0）或录制带语音短视频（duration>0），自动人脸检测标注 |
| `get_volume` | 无 | 获取当前音量（0-100%） |
| `set_volume` | `volume: int (0-100)` | 设置音量 |
| `get_omni_voice` | 无 | 获取当前 TTS 音色 |
| `set_omni_voice` | `voice: 音色名` | 切换 TTS 音色 |
| `get_skill_detail` | `skill_name: str` | 加载技能操作指南 |
| `go_sleep` | 无 | 进入睡眠模式（仅保留唤醒词监听） |
| `execute_shell` | `command: str` | 执行远程命令（代理到 Shell Proxy） |

### CLI 命令（通过 execute_shell 代理）

| 命令 | 说明 |
|------|------|
| `cur-time` | 获取当前日期时间 |
| `weather <城市>` | 查询城市天气 |
| `search <关键词>` | 联网搜索 |
| `ident -u <用户ID>` | 人脸识别核查 |
| `turn -t user\|table` | 机器人转身 |

喂饭/喂水相关命令（`pick bowl`、`scoop`、`deliver-spoon`、`pick cup`、`lower-cup`、`deliver-cup` 等）在执行前需加载对应技能指南。

---

## 音色列表

| 音色 | 风格 | 适用场景 |
|------|------|----------|
| **Serena** | 温柔耐心，清晰 | 日常陪伴、健康提醒（默认推荐） |
| **Mia** | 舒缓从容，慢生活 | 慢节奏陪伴 |
| **Maia** | 知性温柔，亲切自然 | 日常聊天 |
| **Liora Mira** | 烟火温柔，温暖陪伴 | 情感陪伴 |
| **Theo Calm** | 沉稳疗愈 | 情绪安抚、睡前陪伴 |
| **Harvey** | 低沉浑厚，穿透力强 | 听力较弱的长辈 |
| **Ethan** | 阳光朝气 | 晨间问候、康复锻炼鼓励 |
| **Andre** | 磁性沉稳，自然舒服 | 日常健康提醒 |
| **Kiki** | 甜美粤语 | 粤语长辈 |
| **Sunny** | 甜心四川话 | 四川话长辈 |
| **Tina** | 温热奶茶，亲切 | 默认音色 |

---

## 调试与排错

### 调试文件

- **调试文件**：ASR、唤醒词、Omni 音频和 `observe_environment` 媒体文件统一保存到 `debug_files/` 目录
- **临时视频**：录制的视频保存到 `/tmp/voice_video_*.avi`

### 性能指标

每轮对话结束后自动打印延时指标：

```
--- 性能指标 ---
  ASR 延时 (开始说话→句子就绪): 0.850s
  声纹延时: 0.234s
  视频编码延时: 0.567s
  意图识别延时: 0.000s
  Agent 调度延时 (应答开始→API调用): 0.008s
  LLM 首 token 延时 (API调用→首token): 1.234s
  LLM 总耗时 (API调用→生成结束): 3.456s
  首音频延时 (首token→首音): 0.567s
  端到端总延时 (开始说话→首音): 1.801s
---------------
```

### 常见问题

| 现象 | 可能原因 | 排查步骤 |
|------|----------|----------|
| 唤醒词无响应 | 麦克风设备不匹配 | 检查 `MIC_DEVICE_NAME` 是否匹配实际设备名 |
| 唤醒词无响应 | 模型文件缺失 | 检查 `models/wakeup/` 下 4 个文件是否齐全 |
| ASR 识别不准 | API 凭证错误 | 检查 `BYTEDANCE_API_KEY` |
| 摄像头无画面 | Foxglove Bridge 未启动 | 检查 `FOXGLOVE_BRIDGE_URL` 配置，确认机器人端 bridge 进程运行中 |
| 声纹识别失败 | 识别服务未启动 | 检查 `IDENTIFICATION_URL` 配置，确认服务可达 |
| 机器人不动作 | Shell Proxy 未启动 | 检查 `SHELL_PROXY_URL` 配置，确认服务可达 |
| 无语音输出 | 扬声器设备不匹配 | 检查 `SPEAKER_DEVICE_NAME` 是否匹配 |
| 无语音输出 | DashScope API 凭证错误 | 检查 `QWEN_LLM_CONFIG["api_key"]`（即 `DASHSCOPE_API_KEY`） |
| 豆包调用失败 | Ark API Key 未配置 | 检查 `BYTEDANCE_LLM_CONFIG["api_key"]`（即 `ARK_API_KEY`，格式 `ark-` 开头） |
| 语音打断过于敏感 | 能量阈值过低 | 调高 `INTERRUPT_ENERGY_THRESHOLD`（默认 0.8） |
| 语音打断无响应 | 能量阈值过高 | 调低 `INTERRUPT_ENERGY_THRESHOLD` |

---

## 部署清单

部署前逐项确认：

- [ ] Python 3.13+ 已安装，`uv` 已安装并可用
- [ ] `uv pip install -r requirements.txt` 执行成功，无报错
- [ ] sherpa-onnx 唤醒模型已下载到 `models/wakeup/`
- [ ] `.env` 中所有 API 密钥已配置（火山引擎、DashScope、Ark）
- [ ] `.env` 中 `MIC_DEVICE_NAME` / `SPEAKER_DEVICE_NAME` 与实际设备匹配
- [ ] 机器人端 Foxglove Bridge 已启动（检查 `FOXGLOVE_BRIDGE_URL` 配置）
- [ ] 识别服务已启动并通过 health check（检查 `IDENTIFICATION_URL` 配置）
- [ ] Shell 代理服务器已启动（检查 `SHELL_PROXY_URL` 配置）
- [ ] 识别服务中已注册至少一个用户
- [ ] 运行 `uv run python main.py` 验证启动日志无报错
- [ ] 测试完整流程：唤醒词 → 应答 → 说话 → ASR 识别 → Agent 回复 → 语音输出
- [ ] 测试语音打断：在 AI 说话时说话，验证打断是否生效
- [ ] 测试摄像头工具：确认 `observe_environment` 能正常拍照和录像