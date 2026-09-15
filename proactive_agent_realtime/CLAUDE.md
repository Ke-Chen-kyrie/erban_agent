# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

小伴 — 具身智能康养管家系统。唤醒词触发 → Qwen Omni Realtime 长连接多轮对话（音频+视觉）。

- 唤醒词"小伴小伴"唤醒本地 sherpa-onnx KWS
- 唤醒后建立 Qwen Omni Realtime WebSocket 长连接，发送系统提示词+工具定义+摄像头帧+音频
- 双方沉默 20s 或用户说"去睡觉"/go_sleep → 休眠，等待下次唤醒

## Commands

```bash
uv sync                        # 安装依赖
uv run python -u main.py       # 启动
```

所有参数通过 `.env` 配置（`config.py` 读取并做 `validate_config()` 启动校验）。没有测试套件、lint 或格式化工具。

## Environment & Configuration

`.env` 包含所有配置（含 API keys，已在 `.gitignore`，**切勿提交**）。`config.py` 将其读为模块常量，`validate_config()` 启动时校验 `DASHSCOPE_API_KEY`、`OMNI_VOICE`、`CAMERA_SOURCE`。

### 关键配置分组

| 分组 | 变量 | 说明 |
|---|---|---|
| **唤醒词** | `WAKEUP_KEYWORD`, `WAKEUP_MODEL_DIR`, `WAKEUP_COOLDOWN_SECONDS` | 唤醒词文本、sherpa-onnx 模型路径、冷却时间 |
| **音频设备** | `MIC_DEVICE_NAME`, `SPEAKER_DEVICE_NAME` | 按名称匹配（优先于索引，重启后索引可能变化） |
| **摄像头** | `CAMERA_SOURCE` (local/foxglove/zenoh), `CAMERA_FPS` | 三种采集源，详见下方摄像头采集节 |
| **DashScope** | `DASHSCOPE_API_KEY`, `DASHSCOPE_WORKSPACE_ID` | Qwen Omni Realtime API 凭证 |
| **Omni 模型** | `QWEN_OMNI_MODEL`, `QWEN_OMNI_REGION`, `OMNI_VOICE`, `OMNI_SAMPLE_RATE` | 模型选择、区域、TTS 音色、采样率 |
| **对话控制** | `SENTENCE_TIMEOUT` | 双方沉默超时秒数（默认 20） |
| **外部服务** | `IDENTIFICATION_URL`, `JOINT_SERVICE_URL`, `SHELL_PROXY_URL` | 人脸识别、关节推理、Shell 远程执行 |
| **Langfuse** | `LANGFUSE_SECRET_KEY`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_BASE_URL`, `OTEL_EXPORTER_OTLP_CERTIFICATE` |  tracing 上报（使用自签名证书 `certs/langfuse-erban-cert.pem`） |
| **大屏渲染** | `RENDER_SOCKET_ENABLED`, `RENDER_SOCKET_URL` | 渲染 WebSocket 开关和地址 |
| **事件** | `EVENT_PORT`, `EVENT_SERVER_ENABLED`, `ACTION_EVENT_ENABLED` | HTTP 事件接收端口和开关 |
| **音频播放** | `AUDIO_SOURCES` (omni/tts), `AUDIO_DRAIN_SILENCE_MS` | 音频来源和播放后静音 drain 时长 |

`custom_keywords.txt` 由 `WakeupDetector._generate_keywords_file()` 每次启动时自动生成（pypinyin 转中文唤醒词为 sherpa-onnx 音素 token），已在 `.gitignore`。

## Architecture

```
唤醒词检测 (sherpa-onnx KWS, 本地)
      │
      ▼
Qwen Omni Realtime WebSocket 长连接
      │
      ├─ 实时音频流 → 服务端 VAD + ASR → transcription 事件
      ├─ 摄像头帧 → 模型视觉感知
      ├─ 系统事件注入（摔倒检测、天气预警等）
      │
      ▼
模型输出 → 文本 token + 音频 → 本地播放
         → 工具调用 → execute_tool 分发 → 结果回传模型
```

### 会话生命周期

`main.py:WakeupApp._main_loop()` 驱动整个生命周期：

1. **休眠** — `_wakeup_event.wait()` 阻塞，WakeupDetector 线程消费音频队列检测唤醒词
2. **唤醒** — `_wakeup_event.set()`，排出 EventBuffer 中排队事件作为 greeting，构建 system prompt
3. **长连接对话** — 创建 `RealtimeSession`，连接 Qwen Omni，发送音频+视频+事件，消费模型输出
4. **休眠** — `go_sleep` 工具调用或 idle timeout 触发，`_wakeup_event.clear()` 回到步骤 1

### 线程模型（混合 threads + asyncio）

代码库同时使用线程和 asyncio，分层清晰：

- **AudioCapture 线程** — 麦克风采集写入 `queue.Queue`（线程安全）
- **WakeupDetector 线程** — 休眠时从同一个 `queue.Queue` 消费音频，唤醒后让出
- **CameraCapture 线程** — 摄像头采集写入 `queue.LifoQueue(maxsize=1)`，始终提供最新帧
- **QwenRealtimeClient WebSocket 线程** — DashScope SDK 内部线程，通过 `_dispatch()` 同步回调将事件放入 `asyncio.Queue`
- **主 asyncio 事件循环** — `asyncio.run()` 驱动，`run_in_executor()` 桥接线程操作
- **AudioPlayer (PortAudio)** — 阻塞 `stream.write()` 在主事件循环中执行

关键桥接：`agent/utils.py` 通过模块级变量共享跨模块状态（sleep 信号、音频队列引用、TTS fallback、声卡缓存），避免循环导入。

### 音频流水线

```
麦克风 → AudioCapture 线程 → queue.Queue (maxsize=80)
                                    │
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
            WakeupDetector 线程              RealtimeSession._feed_audio_and_video()
            (休眠时消费，唤醒后让出)           (唤醒后消费：发音频 → 取最新摄像头帧 → 发图片)
```

WakeupDetector 在 `is_asr_active()` 或 `is_speaking()` 为 True 时主动让出音频队列（sleep 0.1s + 清空积压），避免与 ASR/打断检测抢音频。

### 音频播放管理器 (`audio/playback_manager.py`)

`AudioPlaybackManager` 封装 AudioPlayer 生命周期，有几个关键设计：

- **死锁避免** — `write()` 的阻塞 PortAudio 调用在锁外执行，使用 `_write_in_progress` 计数器 + `_write_done` 条件变量，`close()` 等待所有 pending write 完成后再获取锁关闭播放器
- **WAV vs raw PCM** — Omni 音频带 RIFF 头（WAV），TTS 音频为 raw int16 PCM。`write()` 检测 RIFF 魔数自动判断，TTS 数据做 int16→float32 转换
- **播放器重试** — 设备繁忙时重试最多 3 次（0.2s/0.5s/1.0s 递增延迟）；连续 5 次失败后停止重试避免日志刷屏
- **写入失败恢复** — 连续 3 次写入失败自动关闭并重建播放器

### VAD 打断与中断处理

服务端 VAD 检测到用户说话时触发 `speech_started`/`speech_stopped` 事件，本地处理流程：

1. **speech_started** → 清空播放器 buffer (`player.clear()`)，取消当前模型响应 (`cancel_response()`)，结束当前 Langfuse Generation 标记为 "interrupted"，设置 `_turn_interrupted = True`
2. **speech_stopped** → 递增 `_turn_index`，开始计时新 turn 的首 token 延迟
3. **工具调用结果丢弃** — 如果 `_turn_interrupted` 或 `speech_started` 为 True，工具结果不发送给模型（旧 turn 已废弃）
4. **`_response_epoch` / `_audio_sent_epoch` 机制** — 防止图片在音频之前发送：`response.done` 时递增 `_response_epoch`，只有 `_audio_sent_epoch == _response_epoch` 时才允许发图片

### go_sleep 特殊处理路径

`go_sleep` 不经过 `execute_tool` 正常流程。在 `realtime_session.py:_handle_tool_call()` 中直接设置 `_sleep_requested = True` 并 `_stop_event.set()`，不发送 function_call_result 回模型。会话结束后 `main.py` 调用 `clear_sleep_request()` 和 `_wakeup_event.clear()` 回到休眠。

### 事件系统

```
外部系统 → POST /event (HTTP, port 8769) → EventServer → EventBuffer
                                                              │
                                              ┌───────────────┴───────────────┐
                                              ▼                               ▼
                                    _wakeup_event.set()              drain_all() 在唤醒后
                                    (触发唤醒)                       注入为 greeting 文本
```

EventBuffer 按 `event_name` 去重（同 key 已在队列则跳过），drain 后清空去重 set。唤醒后 `drain_all()` 排出的所有事件拼入 greeting，同时通过 RenderSocket 发送 `system_event`。

事件注入在中途对话时通过 `_watch_events()` 协程处理：取消当前响应 → 清空音频缓冲 → `send_text(event_text)` → 触发新响应。注入期间设置 `_injection_pending = True` 防止重复注入撞「已激活响应」。

### 工具定义自动生成

`agent/tools.py` 是唯一工具文件，包含三部分：
1. **函数实现** — 8 个工具函数，每个有 docstring 和 type hints
2. **Schema 生成** — `_build_tool()` 用 `inspect` 读 docstring + type hints 自动生成 OpenAI function-calling JSON Schema
3. **运行时 dispatch** — `execute_tool(name, args)` 查 `_TOOL_MAP` 分发执行

新增工具只需：写函数 + 加一行 `_build_tool(func)` 到 `REALTIME_TOOLS` 列表 + 加到 `_TOOL_MAP`。

### 音色切换 (`set_omni_voice`)

`set_omni_voice` 工具调用后，`_handle_tool_call()` 检测结果中的 "已切换"，调用 `await self._client.update_voice(voice)` 发送完整的 `session.update`（`session.update` 是完整覆盖而非 patch，需要带上全部配置）。`agent/voice_state.py` 维护模块级 `_current_voice` 变量供跨模块读取。

### Langfuse 管理的提示词/内容

所有可变文本从 Langfuse 拉取（production 标签），不再本地硬编码：

| Langfuse Prompt 名 | 用途 | 加载位置 |
|---|---|---|
| `proactive_agent_realtime/system_prompt` | 系统提示词 | `prompts.py` |
| `proactive_agent_realtime/execute_shell_tool_description` | execute_shell 工具描述 | `agent/tools.py` |
| `proactive_agent_realtime/langfuse_skill_names` | 技能名列表 | `agent/skill_manager.py` |
| `proactive_agent_realtime/skills/{name}` | 各技能详情 | `agent/skill_manager.py` |

`skill_manager.py` 和 `prompts.py` 使用自定义 CA 证书 `certs/langfuse-erban-cert.pem` 连接自托管 Langfuse 实例。

### Langfuse 追踪

每个 `RealtimeSession` 创建一条 Langfuse Trace，记录会话级元数据（model、voice、region）。每个模型响应 turn 创建一个 Generation span，记录 TTFT、总延迟、token 数、是否被打断。每个工具调用创建一个子 Span，记录耗时和成功/失败状态。

### 技能系统

`agent/skill_manager.py:SkillLoader` 在 import 时从 Langfuse 拉取技能列表和详情：`proactive_agent_realtime/langfuse_skill_names` prompt 列出技能 prompt 名，逐个拉取并解析 YAML frontmatter（`name`/`description`）。`prompts.py:build_system_prompt()` 将技能列表拼入系统提示词。技能内容只存在于 Langfuse，仓库内没有本地技能副本。

### 大屏渲染

`render/render_socket.py` — WebSocket 客户端，连接 `ws://{host}:{port}/render`。发送消息类型：`user_speech`、`agent_token`、`agent_response_start/end/interrupted`、`system_event`。fire-and-forget，队列满自动丢弃。支持断线自动重连（最多 3 次），定期 ping/pong 保活。

### 摄像头采集

`camera_capture.py` — 独立线程采集，支持三种 source（`.env` 中 `CAMERA_SOURCE` 配置）：
- **local** — OpenCV 读本地 `/dev/videoX`
- **foxglove** — Foxglove WebSocket 桥接 ROS 话题
- **zenoh** — Zenoh 订阅（服务端已预标注，跳过人脸检测）

队列容量 1 (LifoQueue)，满时丢弃最旧帧，始终提供最新帧。local/foxglove 模式下帧会经过人脸检测+标注后再入队。

## Known Gotchas

- **P1 — 工具调用+事件注入的 `_turn_interrupted` 时序**：事件注入触发的响应若以工具调用开场（无文本 token），`_turn_interrupted` 仍为 True，工具结果会被误丢弃。修复方式：在 `response_created` 事件中重置 `_turn_interrupted = False`。
- **P2 — 事件注入与用户语音冲突**：用户说话时不注入事件，否则 `clear_audio_buffer` 会清掉用户语音，`cancel` 与 VAD 自动 turn 冲突。事件留在 buffer 等下一轮。
- **P3 — 会话结束与唤醒事件竞争**：`_main_loop()` 中会话结束后、`_wakeup_event.clear()` 前到达的事件可能丢失。修复方式：`clear()` 后检查 `_event_buffer.pending()`，如有则重新 `_wakeup_event.set()`。
- **P5 — session.updated 未确认则终止**：`qwen_realtime_client.py` 中若 5 秒内未收到 `session.updated` 确认，终止会话避免在错误配置下运行（无 tools、VAD 模式错误）。
- **`session.update` 是完整覆盖**：更新 voice 时必须发送完整 session 配置，不是 patch。
- **`audio/__init__.py` 使用 `__getattr__` 懒加载** `AudioPlaybackManager` 以打破与 `agent/utils.py` 的循环导入。

## Key Source Files

| 文件 | 说明 |
|---|---|
| `main.py` | 唯一入口。WakeupApp 驱动唤醒→对话→休眠循环 |
| `config.py` | 所有配置（`.env` → 模块常量），含 `validate_config()` |
| `realtime_session.py` | 长连接会话：音频/视频发送、模型输出消费、工具调用、VAD 打断、超时休眠、Langfuse 追踪 |
| `qwen_realtime_client.py` | DashScope Qwen Omni Realtime WebSocket 客户端（基于官方 SDK） |
| `agent/tools.py` | 工具实现 + schema 生成 + dispatch（三合一） |
| `agent/skill_manager.py` | 从 Langfuse 拉取技能列表和详情 |
| `agent/utils.py` | 跨模块共享状态：sleep 信号、音频队列引用、TTS fallback、声卡缓存 |
| `agent/voice_state.py` | 当前 Omni 音色状态（get/set） |
| `prompts.py` | 从 Langfuse 拉取系统提示词，`build_system_prompt()` 拼装用户信息+技能列表 |
| `render/render_socket.py` | 大屏渲染 WebSocket 客户端（fire-and-forget，自动重连） |
| `audio/playback_manager.py` | 音频播放生命周期管理：ensure_player/open/close、WAV vs raw PCM 格式转换、死锁避免 |
| `audio/audio_output.py` | AudioPlayer 封装（PortAudio/sounddevice） |
| `wakeup_detector.py` | sherpa-onnx 本地唤醒词检测，含冷却机制和音频让出逻辑 |
| `camera_capture.py` | 摄像头采集线程，支持 local/foxglove/zenoh 三种源 |
| `foxglove_client.py` | Foxglove WebSocket 摄像头客户端 |
| `zenoh_client.py` | Zenoh 摄像头订阅客户端 |
| `events/` | EventBuffer（去重缓冲）+ EventServer（HTTP 接收外部事件） |
| `vision/` | 人脸检测 + 身份识别客户端 |
| `logging_config.py` | 统一日志配置（`get_logger()` + `setup()`） |

## Dependencies

`pyproject.toml` 要求 Python >= 3.10，依赖分组：

- **核心**: httpx, websockets, python-dotenv, numpy, Pillow, pyyaml
- **音频**: pyaudio, sounddevice, soundfile, scipy
- **LLM/Agent**: dashscope, openai, langfuse
- **唤醒词**: sherpa-onnx, pypinyin
- **视觉**: opencv-python
- **ROS2**: rosbags
- **Zenoh**: eclipse-zenoh
- **其他**: aiohttp, aiortc, av, certifi, oss2, modelscope, torch