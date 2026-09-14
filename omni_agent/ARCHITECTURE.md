# "小伴" 具身智能康养机器人 —— 系统架构文档

## 一、系统概览

"小伴"是由迩伴智能机器人（深圳）有限公司研发的具身智能康养语音助手，面向养老院场景，为老人提供日常陪伴、知识问答和物理辅助（喂饭、喂水等）。系统通过麦克风、摄像头和外部传感器感知环境，经由 LLM 智能体进行多模态推理决策，最终以语音合成和机器人控制指令完成执行闭环。

### Agent 功能清单

"小伴" Agent 具备以下核心能力：

#### 语音对话
- **唤醒词触发**：说出"小伴小伴"唤醒机器人，进入对话模式
- **多轮对话**：支持自然语言连续对话，Agent 保持上下文记忆
- **语音打断**：用户可在 AI 播放期间随时插话打断，Agent 停止播报并切换至聆听状态
- **睡眠模式**：用户说"闭嘴"、"去睡觉"、"休息吧"等，Agent 播报告别语后进入睡眠，仅保留唤醒词和系统事件监听

#### 视觉理解
- **伴随语音画面分析**：用户说话时，Agent 自动接收摄像头画面，理解场景中的人物、物品、动作
- **静默手势交互**：支持 13 种手势（点头/摇头/挥手/捂胸/捂头/捂腹/张嘴/揉眼/OK/点赞/举手/停止/剪刀手），无需语音即可表达意图，系统在监听窗口内以 2s 为周期轮询动作事件
- **主动环境观察**：Agent 可调用摄像头主动抓拍或录制短视频，用于身份核实、安全检查和场景确认

#### 身份识别
- **声纹识别**：通过语音自动识别说话人身份，区分老人、护工、管理员和路人
- **人脸识别**：画面中的人物自动标注身份（绿框=已注册用户，红框=陌生人），配合声纹综合判定交互对象

#### 物理操作（需加载安全 SOP）
- **喂饭**：机械臂取食并送至用户嘴边，支持多轮喂食
- **喂水**：机械臂取水杯并送至用户嘴边

#### 信息查询
- **天气查询**：查询当前或指定城市天气
- **时间查询**：查询当前时间、日期
- **联网搜索**：回答用户知识性问题

#### 主动关怀
- **系统事件响应**：接收第三方传感器事件（摔倒检测、下雨提醒、设备告警等），通过 HTTP POST 推送至 `/system_event` 端口。支持三种注入路径：睡眠态立即唤醒并主动关怀、对话中合并到用户消息、TTS 后独立处理。事件附带图片/视频自动经过人脸检测标注
- **视觉主动感知**：可根据对话上下文主动观察环境，判断用户状态

#### 设备控制
- **音量调节**：查询和设置扬声器音量（0-100%）
- **音色切换**：支持 11 种语音音色（含方言音色），适应不同场景和用户偏好

#### 安全机制
- **物理操作前置检查**：喂饭、喂水等操作必须先加载技能指南（SKILL.md），严格按 SOP 执行
- **命令安全过滤**：所有机器人控制命令经过注入检测和危险操作拦截
- **身份校验**：物理操作前通过声纹+人脸双重确认用户身份

---

## 二、三层架构总览

```
┌──────────────────────────────────────────────────────────────────┐
│                        感知层 (Perception)                        │
│                                                                  │
│   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌────────────┐   ┌────────────┐  │
│   │  音频采集  │   │  语音识别  │   │  视频采集  │   │  系统事件   │   │  动作事件   │  │
│   │  (16kHz)  │   │ (ASR)    │   │ (ROS2)   │   │  (HTTP)    │   │  (HTTP)    │  │
│   └─────┬─────┘   └────┬─────┘   └─────┬─────┘   └─────┬──────┘   └─────┬──────┘  │
│         │              │               │               │               │         │
│         └──────────────┴───────────────┴───────────────┴───────────────┘         │
│                              │                                                   │
│                    PerceptEnvironment                                            │
│                    (感知状态机)                                                   │
│              产出: SpeechEvent | GestureEvent | TimeoutEvent                     │
└──────────────────────────────────────────────────────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │  user_text         │  audio_base64      │  video_frames
          ▼                    ▼                    ▼
┌──────────────────────────────────────────────────────────────────┐
│                        决策层 (Decision)                          │
│                                                                  │
│   ┌─────────────────────────────────────────────────────────┐   │
│   │                    LangGraph Agent                       │   │
│   │                                                         │   │
│   │   记忆压缩 ──→ 多模态推理 ──→ 工具调用 ──→ 摄像头注入     │   │
│   │                                                         │   │
│   │   输入: 语音+视频+身份+对话历史                            │   │
│   │   输出: 文本回复 + 音频输出 + 工具调用指令                   │   │
│   └─────────────────────────────────────────────────────────┘   │
│                                                                  │
│   ┌──────────────┐    ┌──────────────┐    ┌──────────────┐      │
│   │ 多模态路由    │    │  技能系统     │    │  LLM 后端    │      │
│   │ 视觉/意图判  │    │  安全SOP     │    │ 豆包/Qwen    │      │
│   └──────────────┘    └──────────────┘    └──────────────┘      │
└──────────────────────────────────────────────────────────────────┘
                               │
          ┌────────────────────┼────────────────────┐
          │  text              │  audio_pcm         │  tool_calls
          ▼                    ▼                    ▼
┌──────────────────────────────────────────────────────────────────┐
│                        执行层 (Execution)                         │
│                                                                  │
│   ┌──────────┐   ┌──────────┐   ┌──────────┐   ┌──────────┐    │
│   │  语音合成  │   │  音频播放  │   │ 机器人控制 │   │ 设备控制   │    │
│   │  (TTS)   │   │ (Speaker)│   │(Shell代理)│   │(音量/音色) │    │
│   └──────────┘   └──────────┘   └──────────┘   └──────────┘    │
└──────────────────────────────────────────────────────────────────┘
```

---

## 三、感知层

感知层负责从环境中采集多模态信息，经处理后产出结构化感知事件，供决策层消费。

### 3.1 功能概述

| 子系统 | 功能 | 输入 | 输出 |
|--------|------|------|------|
| 音频采集 | 麦克风 16kHz 持续收音 | 环境声音 | PCM 音频帧队列 |
| 唤醒词检测 | 本地离线关键词识别 | 音频帧 | 唤醒信号 + 音频 WAV |
| 语音识别 | 语音转文字（云端/本地） | 音频帧 | 文本句子 |
| 视频采集 | ROS2 摄像头画面录制 | 压缩图像 | 视频帧列表 |
| 动作检测 | 手势识别（13 种手势，外部视觉服务推送） | HTTP POST | 动作类型 + 置信度 + 人脸截图 |
| 声纹识别 | 说话人身份判定 | 音频 WAV | user_id |
| 人脸识别 | 画面中人物身份判定 | 图像帧 | user_id + 人脸框标注 |
| 环境观察 | Agent 主动感知（抓拍/录像/录音） | 工具调用 | 图像/视频/音频 + 人脸标注 |
| 系统事件 | 第三方传感器事件接收 | HTTP POST | 事件文本 |
| 动作事件 | 手势/动作检测事件接收 | HTTP POST | 动作类型 + 置信度 + 人脸截图 |
| 打断检测 | 用户插话实时检测 | 音频帧 | 打断信号 |

### 3.2 音频数据流

```
麦克风 ──→ AudioCapture ──→ audio_queue ──┬──→ WakeupDetector (持续监听唤醒词)
                                          ├──→ ASR (唤醒后识别用户语音)
                                          └──→ VoiceInterruptDetector (AI讲话时监听打断)
```

**时序逻辑**：
1. **睡眠态**：WakeupDetector 独占音频队列，逐帧检测唤醒词，其他消费者空闲
2. **唤醒瞬间**：WakeupDetector 导出 3 秒滚动缓冲区的 WAV 音频，触发唤醒回调
3. **活跃态**：ASR 接管音频队列，进行实时语音识别，WakeupDetector 让出音频
4. **AI 播放态**：VoiceInterruptDetector 接管音频队列，检测用户插话，ASR 暂停
5. **打断后**：清空缓冲音频，重新开始 ASR 监听

### 3.3 视频数据流

```
ROS2 摄像头 ──→ Foxglove WebSocket ──→ VideoRecorder ──→ 内存帧列表
                                            │
                                    stop_and_restart()
                                    按句子边界分段
```

**时序逻辑**：
1. 唤醒后预启动视频录制（`prepare_video`），避免首帧等待
2. `percept.start()` 启动后持续录制，ASR 检测到句子结束 → `stop_and_restart()` 原子分段，当前段帧列表交给决策层
3. Agent 处理期间暂停录制，完成后恢复
4. 视频编码在决策层并行执行：运动峰值帧提取 → 人脸标注 → 重编码

### 3.4 系统事件数据流

系统事件是外部第三方传感器（摔倒检测、下雨提醒、设备告警等）通过 HTTP 推送至"小伴"的异步通知机制。系统事件可在任意时刻到达，根据当前系统状态有不同的处理路径。

**事件接收**（`system_event_server.py`）：

`on_system_event` 回调始终将事件放入 `_system_event_queue` 并设置 `_wakeup_event`（无条件）。在非 SLEEP 态下设置 wakeup_event 无实际影响，主循环不会等待它。

```
外部传感器 ──→ HTTP POST /system_event (端口 8769) ──→ SystemEventServer
                                                           │
                                              callback(SystemEvent)
                                                           │
                                              _system_event_queue.put()
                                              _wakeup_event.set()  (无条件)
```

**SystemEvent 数据结构**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `text` | `str` | 格式化事件文本：`[系统事件] 类型: {event_type}，描述: {description}` |
| `images` | `list[str]` | 可选的 base64 图片列表 |
| `videos` | `list[str]` | 可选的 base64 视频列表 |

**HTTP 请求格式**：
```json
POST /system_event
{
  "event": "fall_detected",        // 必填：事件类型（任意字符串）
  "description": "检测到老人摔倒",   // 必填：事件描述
  "images": ["<base64>", ...],     // 可选
  "videos": ["<base64>", ...]      // 可选
}
```

**三种注入时机**：

系统事件根据到达时机，有三种不同的注入 Agent 的路径：

```
系统事件到达
  │
  ├── 睡眠态（SLEEP）
  │      → 设置 _wakeup_event，唤醒主循环
  │      → 跳过声纹识别（user_id = "unknown"）
  │      → 直接作为首条消息送入 Agent（greeting = 事件文本）
  │
  ├── 对话中（Agent 处理 / TTS 播放期间）
  │      → 暂存于 _system_event_queue
  │      → 下一轮 _handle_speech_event 时合并到用户消息前面
  │      → 或 TTS 播放完毕后由 _process_pending_system_events 独立处理
  │
  └── 感知监听中（LISTENING）
         → 暂存于 _system_event_queue
         → 下一轮 SpeechEvent 或 GestureEvent 处理时合并
```

**媒体标注**（`pipeline.py:annotate_system_event_media`）：

系统事件附带的图片/视频在送入 Agent 前会经过人脸检测标注：
- 对所有图片做人脸检测
- 对视频的首帧和末帧做人脸检测
- 在人脸位置绘制标注框（绿框=已注册用户，红框=陌生人）
- 生成人脸摘要文本（如 `[人脸识别: 视野中有 佩林(已注册), 路人(未注册)]`），前置到事件文本中

**事件过滤**（`main.py:_filter_system_events`）：

与动作事件类型重名的系统事件（如 `nod`、`wave`、`chest_pain` 等 13 种）会被丢弃，避免重复处理——这些类型应走动作事件管道。

**TTS 后独立处理**（`main.py:_process_pending_system_events`）：

在每轮 AI 应答的 TTS 播放完毕后、恢复感知监听前，检查并处理排队中的系统事件。此路径会：
1. 保存当前用户身份信息
2. 清空用户身份（`user_id = "unknown"`），以系统角色发送事件
3. 调用 Agent 处理事件（强制使用大模型 + 发送视觉）
4. 恢复之前的用户身份信息
5. 如果 Agent 请求睡眠，则停止感知环境回到 SLEEP

### 3.5 动作事件数据流

动作事件是外部视觉检测服务（基于 MediaPipe + 手势识别模型）通过 HTTP 推送至"小伴"的静默手势交互机制。与语音不同，手势交互无需用户开口说话，系统在监听窗口内以 2 秒为周期轮询动作事件队列。

**事件接收**（`action_event_server.py`）：

```
外部视觉检测服务 ──→ HTTP POST /action_event (端口 8770) ──→ ActionEventServer
                                                                   │
                                                    _action_event_queue.put(ActionEvent)
```

**ActionEvent 数据结构**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `action_type` | `str` | 动作类型，必须是 `VALID_ACTIONS` 中的一种 |
| `confidence` | `float` | 检测置信度 |
| `face_image` | `str` | 可选的 base64 人脸截图，用于身份识别 |

**HTTP 请求格式**：
```json
POST /action_event
{
  "action_type": "wave",       // 必填：动作类型
  "confidence": 0.95,          // 必填：置信度
  "face_image": "<base64>"     // 可选：动作时刻人脸截图
}
```

**支持的 13 种动作类型**（`VALID_ACTIONS`）：

| 动作类型 | 中文名 | 含义 |
|---------|--------|------|
| `nod` | 点头 | 确认、同意、肯定 |
| `shake` | 摇头 | 拒绝、否定、不同意 |
| `wave` | 挥手 | 打招呼、告别、引起注意 |
| `chest_pain` | 捂胸 | 胸口不适/疼痛 |
| `head_pain` | 捂头 | 头部不适/疼痛 |
| `abdomen_pain` | 捂腹 | 腹部不适/疼痛 |
| `mouth_open` | 张嘴 | 需要喂食/张嘴配合 |
| `eye_rub` | 揉眼 | 眼部不适/困倦 |
| `ok` | OK | 确认、没问题 |
| `thumbs_up` | 点赞 | 满意、称赞 |
| `raise_hand` | 举手 | 请求关注、求助 |
| `stop` | 停止 | 暂停当前动作 |
| `peace` | 剪刀手 | 轻松、愉快 |

**事件队列共享**：

`ActionEventServer` 和 `PerceptEnvironment` 共享同一个 `_action_event_queue`（`queue.Queue`）。服务器线程写入，感知环境轮询读取，确保了线程安全。

**语音优先策略**：

PerceptEnvironment 的 `wait_for_input()` 实现了严格的语音优先：
- 每 100ms 优先检查 ASR 句子队列，有语音立即返回 `SpeechEvent`
- 每 2s 检查动作事件队列（仅当用户未说过话时）
- 如果 ASR 检测到语音开始（`_on_speech_begin` 回调），立即清空动作队列
- 如果在同一轮中用户已经说过话，2s 轮询时也清空动作队列

**GestureEvent 构造流程**：

```
动作事件命中
  │
  ├── 停止视频录制（获取感知窗口内的全部帧）
  ├── 停用 ASR（deactivate_listening）
  ├── 保存环境音频 WAV（通常为空）
  ├── 人脸识别：
  │     ├── 优先使用 action_event 携带的 face_image
  │     └── 否则取视频最后一帧
  │     → 返回 (user_name, user_id, user_role, user_description)
  │
  └── 构造 GestureEvent:
        gesture_type = GESTURE_MAPPING[action_type]  (如 "nod" → "点头")
        audio_b64 = base64 环境音频
        video_frames = 感知窗口帧列表
        user_name / user_id / user_role / user_description = 人脸识别结果
```

**手势事件处理**（`main.py:_handle_gesture_event`）：

```
GestureEvent 到达
  │
  ├── pause 感知环境
  ├── 设置用户身份（来自人脸识别）
  ├── 编码视频帧（使用更多关键帧: VIDEO_KEY_FRAME_MAX * 4）
  ├── 构造手势文本（含详细含义描述）:
  │     "[手势交互] 用户对机器人点头，表示确认、同意或肯定。"
  ├── 调用 _stream_response():
  │     gesture_mode=True  → Agent 中用户消息显示为 [多模态消息]
  │     model_size="large" → 强制使用大模型
  │     send_visual=True   → 始终发送视频
  ├── 处理排队系统事件（_process_pending_system_events）
  └── resume 感知环境
```

**动作队列清空时机**：

| 时机 | 原因 |
|------|------|
| `PerceptEnvironment.start()` | 清空 Agent 处理期间积压的事件 |
| `PerceptEnvironment.resume()` | 清空 Agent 处理期间积压的事件 |
| `_on_speech_begin` 回调 | 语音优先，用户已开始说话 |
| 2s 轮询时用户已说过话 | 语音优先，丢弃动作事件 |

### 3.6 感知状态机

PerceptEnvironment 统一调度 ASR、VideoRecorder 和动作事件队列的生命周期：

```
                    ┌──────────────┐
                    │   SLEEP      │
                    │ (等待唤醒)    │
                    └──────┬───────┘
                           │ 语音唤醒 / 系统事件唤醒
                           ▼
                    ┌──────────────┐
                    │   GREETING   │
                    │ (唤醒应答)    │
                    │ prepare_video│  ← 预启动摄像头
                    └──────┬───────┘
                           │
                           ▼
                    ┌──────────────┐
                    │   LISTENING  │
                    │ ASR+Video ON │  ← start(): 激活 ASR + 视频录制，清空动作队列
                    │ action_queue │
                    └──────┬───────┘
                           │ wait_for_input()
                           │
                           │  每 100ms: 检查 ASR 句子队列（语音优先）
                           │  每 2s:   检查动作事件队列（静默手势兜底）
                           │  总超时:  SENTENCE_TIMEOUT (20s)
                           │
              ┌────────────┼────────────┐
              ▼            ▼            ▼
        SpeechEvent   GestureEvent  TimeoutEvent
        (用户说话)    (手势交互)     (20s超时)
              │            │            │
              ▼            ▼            ▼
        ┌──────────────────────────────────────┐
        │          Agent 处理中                 │
        │  ASR+Video PAUSE                     │
        │                                      │
        │  SpeechEvent 并行任务:                │
        │    ├── 声纹识别 (voiceprint)          │
        │    ├── 视频编码 (encode_media)         │
        │    ├── 视觉路由 (route)               │
        │    ├── 意图检测 (physical_intent)    │
        │    └── 对话对象判断 (directed_intent)  │
        │                                      │
        │  GestureEvent 任务（感知层已完成人脸识别）:│
        │    └── 视频编码 (encode_media)         │
        │                                      │
        │  Agent 可调用:                        │
        │    observe_environment ──→ 抓拍/录像   │
        │    execute_shell ────────→ 机器人控制  │
        └──────────────┬───────────────────────┘
                       │
              ┌────────┴────────┐
              ▼                 ▼
        正常完成              go_sleep
        resume()             stop()
        恢复 LISTENING       回到 SLEEP
```

**SpeechEvent 触发后的并行处理**（5 个任务并发）：

```
语音识别完成
  │
  ├── 声纹识别 (voiceprint)      ──→ user_id, user_name, user_role
  ├── 视频编码 (encode_media)     ──→ base64 视频/图片
  ├── 视觉路由 (route)            ──→ send_visual (是否发送画面)
  ├── 意图检测 (physical_intent)   ──→ model_size (large/small)
  └── 对话对象判断 (directed)     ──→ is_directed (是否对小伴说话)
  │
  └── 全部完成 → 组装多模态消息 → Agent 推理
```

**GestureEvent 触发后的处理**（人脸识别已在感知层完成）：

```
动作事件命中（PerceptEnvironment 已构造 GestureEvent，含人脸识别结果）
  │
  ├── 视频编码 (更多关键帧: VIDEO_KEY_FRAME_MAX * 4)
  ├── 构造手势文本 (含 13 种手势的详细含义描述)
  │
  └── Agent 推理:
        gesture_mode=True  → 用户消息显示为 [多模态消息]
        model_size="large" → 强制大模型
        send_visual=True   → 始终发送视频
```

**关键状态转换方法**：

| 方法 | 动作 |
|------|------|
| `prepare_video()` | 预启动视频录制（GREETING 期间并行调用，避免首帧等待） |
| `start()` | 激活 ASR + 启动 VideoRecorder，清空动作队列，包装 speech_begin 回调 |
| `stop()` | 停用 ASR，停止视频，清空句子队列，恢复原始回调 |
| `pause()` | 暂停 ASR（视频已在 wait_for_input 返回时停止） |
| `resume()` | 恢复 ASR（SpeechEvent: 仅 resume；GestureEvent: 重新 activate + 启动线程），重启视频，清空动作队列 |
| `drain_audio()` | 清空音频队列中残留的音频帧 |
| `wait_for_input()` | 核心异步轮询循环，返回 `SpeechEvent` / `GestureEvent` / `TimeoutEvent` |

---

## 四、决策层

决策层是系统的核心大脑。接收感知层产出的结构化数据（文本、音频、视频、身份），通过 LLM 进行多模态理解与推理，输出文本回复、语音和工具调用指令。

### 4.1 功能概述

| 子系统 | 功能 |
|--------|------|
| 多模态路由 | 判断用户输入是否需要视觉理解，降低不必要 token 消耗 |
| 意图检测 | 判断用户是否有喂饭/喂水/康复运动等物理操作意图，决定模型规模 |
| LangGraph Agent | 有状态的多轮对话推理引擎，支持工具调用 |
| 记忆压缩 | 长对话自动摘要，控制上下文窗口 |
| 技能系统 | 物理操作安全 SOP，按需加载 |
| 性能度量 | 全链路延迟追踪（ASR、声纹、视频编码、路由、LLM 首 token/首音频、端到端） |

### 4.2 Agent 推理流程

```
用户输入 (文本 + 音频 + 视频 + 身份)
  │
  ▼
┌─────────────────┐
│  memory_compress │  检查是否需要压缩历史消息
│  (超过15轮触发)  │  → 调用轻量模型生成滚动摘要
└────────┬────────┘
         │
         ▼
┌─────────────────┐
│   omni_node     │  构建多模态消息 → 调用 LLM API
│   (核心推理)     │  → 流式输出文本 + 音频
└────────┬────────┘
         │
    ┌────┴────┐
    │ 有工具调用? │
    └────┬────┘
    No   │   Yes
    ▼    │   ▼
  END    │ ┌──────────┐
         │ │  tools   │  执行工具 (execute_shell / observe_environment 等)
         │ └────┬─────┘
         │      ▼
         │ ┌──────────────┐
         │ │inject_camera │  将工具捕获的摄像头数据注入状态
         │ └──────┬───────┘
         │        │
         │        ▼
         │   回到 omni_node (循环)
```

### 4.3 多模态路由

两套路由系统在 Agent 推理前并行执行，优化推理成本和模型选择：

**视觉路由** — 决定是否发送摄像头画面给 LLM
- Tier 1：关键词匹配（零延迟），覆盖 92 个中文视觉关键词
- Tier 2：小模型语义判断（~100ms），处理模糊输入

**意图检测** — 决定模型规模
- 物理操作（喂饭/喂水/康复运动）→ 大模型，强推理能力
- 普通闲聊 → 小模型，低延迟低成本

**路由控制开关**：

| 环境变量 | 默认值 | 功能 |
|---------|--------|------|
| `ROUTER_ENABLED` | `true` | 意图路由总开关 |
| `ROUTER_VISUAL_ENABLED` | `true` | 视觉路由开关 |
| `ROUTER_VISUAL_FALLBACK_ENABLED` | `false` | Tier 2 小模型语义回退开关 |
| `ROUTER_FEEDING_FALLBACK_ENABLED` | `false` | 喂饭/喂水意图检测小模型回退开关 |

### 4.4 记忆压缩

长对话上下文管理策略：

```
消息 1..30        消息 31..44      消息 45..60
└── 已压缩为摘要 ──┘  └── 保留完整 ──┘  └── 最新轮 ──┘
     (~500字)          (15轮以内)        (永远保留)
```

- 超过 15 条未压缩人类消息时触发（按 HumanMessage 计数，非轮次）
- 调用独立轻量模型（qwen3.6-35b-a3b）生成中文摘要
- 工具链中途不触发，避免压缩不完整对话
- 摘要注入系统提示词，作为对话上下文

### 4.5 技能系统

物理操作的安全管理机制：

- 技能以 SKILL.md 文件形式存储，包含 YAML 元数据 + Markdown 操作指南
- 图构建时技能简介注入系统提示词，Agent 知道有哪些技能可用
- 执行物理操作前必须调用 `get_skill_detail` 加载完整 SOP
- 当前技能：喂饭（feed-food）、喂水（feed-water）、康复运动（rehab-exercise）

---

## 五、执行层

执行层将决策层的推理结果转化为物理世界的动作：语音输出、机器人控制、设备调节。

### 5.1 功能概述

| 子系统 | 功能 | 实现 |
|--------|------|------|
| 语音合成 | 文本→语音 | ByteDance Seed-TTS 2.0 / Qwen-Omni 原生音频 |
| 音频播放 | PCM→扬声器 | sounddevice OutputStream，自适应采样率 |
| 机器人控制 | 命令→物理动作 | HTTP 代理 → Shell 命令 → 机器人 CLI 工具 |
| 设备控制 | 音量/音色调节 | ALSA mixer / Omni 音色切换 |

### 5.2 语音输出数据流

```
Agent 输出文本 token
  │
  ├── TTS 模式 (AUDIO_SOURCES="tts")
  │     token → ByteDance TTS WebSocket → PCM → AudioPlayer → 扬声器
  │
  └── Omni 模式 (AUDIO_SOURCES="omni")
        token → Qwen-Omni 直接输出音频 → PCM → AudioPlayer → 扬声器
```

**TTS Fallback**：豆包 TTS 初始化失败时自动切换到 Omni 模式，后续轮次无需重启。

### 5.3 机器人控制数据流

```
Agent 生成 tool_call
  │
  └── execute_shell("water start -u zhouziqi")
        │
        ▼
  安全检查 (防注入/防危险命令)
        │
        ▼
  HTTP POST → Shell Proxy (192.168.217.100:8088/run)
        │
        ▼
  机器人 CLI 工具 → WebSocket → 机器人执行
```

### 5.4 工具调用时序

```
用户: "帮我倒杯水"
  │
  ▼
Router 检测喂水意图 → model_size = "large"
  │
  ▼
Agent 调用 get_skill_detail("feed-water")  ← 加载操作 SOP
  │
  ▼
Agent 调用 execute_shell("water start -u zhouziqi")  ← 执行物理动作
  │
  ▼
Agent 回复: "好的，正在给您倒水"  ← TTS 播报
```

---

## 六、完整对话时序

以下是一次典型语音对话的完整时序：

```
时间轴 ──────────────────────────────────────────────────────────→

 SLEEP          GREETING           LISTENING          AGENT          PLAYBACK
   │                │                  │                 │               │
   │  唤醒词检测     │                  │                 │               │
   │  "小伴小伴"     │                  │                 │               │
   │ ─────────────→ │                  │                 │               │
   │                │  声纹识别         │                 │               │
   │                │  获取用户信息      │                 │               │
   │                │ ───────────────→ │                 │               │
   │                │  prepare_video    │                 │               │
   │                │  (预启动摄像头)     │                 │               │
   │                │ ───────────────→ │                 │               │
   │                │  发送"你好，小伴！"  │                 │               │
   │                │ ──────────────────────────────────→ │               │
   │                │                  │  Agent 应答      │               │
   │                │                  │ ←────────────────│               │
   │                │                  │                 │  TTS 播放     │
   │                │                  │                 │ ───────────→ │
   │                │                  │  启动 ASR+Video  │               │
   │                │                  │ ───────────────→ │               │
   │                │                  │                 │               │
   │                │                  │  用户说话        │               │
   │                │                  │  "今天天气怎么样"  │               │
   │                │                  │ ───────────────→ │               │
   │                │                  │  ┌─ 声纹识别 ────┤               │
   │                │                  │  ├─ 视频编码 ────┤               │
   │                │                  │  ├─ 视觉路由 ────┤ (5 路并行)    │
   │                │                  │  ├─ 意图检测 ────┤               │
   │                │                  │  └─ 对话对象判断 ─┤               │
   │                │                  │ ───────────────→ │               │
   │                │                  │                 │  Agent 推理   │
   │                │                  │                 │ ───────────→ │
   │                │                  │                 │  TTS 播放     │
   │                │                  │                 │ ───────────→ │
   │                │                  │                 │               │
   │                │                  │  ... 持续对话 ... │               │
   │                │                  │                 │               │
   │                │                  │  20s 无人说话     │               │
   │                │                  │ ───────────────→ │               │
   │                │                  │  TimeoutEvent    │               │
   │ ←─────────────────────────────────┘                 │               │
   │  回到 SLEEP                                         │               │
```

### 打断处理时序

```
PLAYBACK 期间:
  用户突然说话 ──→ VoiceInterruptDetector 检测到 ──→ 设置 _interrupted
                                                      │
  停止 TTS 播放 ←─────────────────────────────────────┘
  保存已播报的部分文本
  清空缓冲音频，恢复 ASR 监听
  用户语音被识别 → 正常 Agent 处理流程
```

---

## 七、系统事件与动作事件时序

### 7.1 系统事件三种注入路径

系统事件可在任意时刻到达，根据当前系统状态有三种不同的处理路径：

```
系统事件到达（HTTP POST /system_event）
  │
  ├── 路径 1: SLEEP 态唤醒
  │     _system_event_queue.put() → _wakeup_event.set()
  │     主循环唤醒 → 跳过声纹识别 (user_id="unknown")
  │     → 媒体标注 (annotate_system_event_media)
  │     → 作为首条 greeting 消息送入 Agent
  │     → Agent 主动关怀应答 → TTS 播报 → 恢复 LISTENING
  │
  ├── 路径 2: 对话进行中（Agent 推理 / TTS 播放中）
  │     _system_event_queue.put()（不唤醒，已在活跃态）
  │     下一轮 SpeechEvent 处理时：
  │       _filter_system_events() 过滤动作重叠事件
  │       合并到用户句子前面: "{event_text}\n{user_sentence}"
  │     → Agent 同时处理事件 + 用户语音
  │
  └── 路径 3: TTS 播放完毕后（_process_pending_system_events）
        _filter_system_events() 过滤动作重叠事件
        保存当前用户身份 → 清空身份 (user_id="unknown")
        → 媒体标注 → 独立 Agent 推理（large model + send_visual）
        → 恢复用户身份
        → 如果 sleep: 停止感知 → SLEEP
        → 否则: 恢复感知监听
```

### 7.2 系统事件唤醒完整时序

```
 SLEEP                     GREETING           LISTENING
   │                          │                   │
   │  HTTP POST 到达           │                   │
   │  "[系统事件] 类型:xxx,     │                   │
   │   描述:检测到摔倒"          │                   │
   │ ────────────────────────→ │                   │
   │                          │  跳过声纹识别       │
   │                          │  prepare_video      │
   │                          │  媒体标注(人脸检测)   │
   │                          │ ─────────────────→ │
   │                          │  发送"[系统事件]    │
   │                          │  类型:xxx,描述:xxx" │
   │                          │ ─────────────────→ │
   │                          │  Agent 主动关怀     │
   │                          │  "奶奶您没事吧？     │
   │                          │   我马上通知护工"    │
   │                          │ ←─────────────────│
   │                          │  TTS 播报           │
   │                          │ ─────────────────→ │
   │                          │  启动 ASR           │
   │                          │  等待用户回复        │
```

### 7.3 动作事件（手势交互）时序

```
 LISTENING                         AGENT                      PLAYBACK
   │                                  │                           │
   │  ASR 监听 + 视频录制              │                           │
   │  每 100ms 检查语音（优先）         │                           │
   │  每 2s 检查动作队列               │                           │
   │                                  │                           │
   │  ActionEvent 到达 (wave)          │                           │
   │ ───────────────────────────────→ │                           │
   │  GestureEvent 构造:              │                           │
   │    停止视频录制                    │                           │
   │    停用 ASR                       │                           │
   │    人脸识别 (face_image)           │                           │
   │ ───────────────────────────────→ │                           │
   │                                  │  Agent 推理                │
   │                                  │  gesture_mode=True         │
   │                                  │  model_size="large"        │
   │                                  │ ────────────────────────→ │
   │                                  │  TTS 播报                  │
   │                                  │  "你好呀！有什么需要       │
   │                                  │   帮忙的吗？"              │
   │                                  │ ←────────────────────────│
   │  恢复感知环境 (resume)             │                           │
   │  ASR 重新激活 + 视频重启           │                           │
```

### 7.4 语音优先与动作队列管理

PerceptEnvironment 的 `wait_for_input()` 实现了严格的语音优先策略：

```
wait_for_input() 轮询循环
  │
  ├── 每 100ms: get_sentence()
  │     有句子 → 立即返回 SpeechEvent（语音绝对优先）
  │
  ├── 每 2s: _try_get_action()
  │     ├── 用户已说过话 (_last_speech_time > 0) → _drain_action_queue() 丢弃
  │     └── 用户未说话 → 有动作 → 返回 GestureEvent
  │
  └── 总超时 SENTENCE_TIMEOUT (20s) → TimeoutEvent → SLEEP
```

**动作队列清空时机汇总**：

| 时机 | 触发方法 | 原因 |
|------|---------|------|
| 感知启动 | `start()` | 清空 Agent 处理期间积压 |
| 感知恢复 | `resume()` | 清空 Agent 处理期间积压 |
| 语音开始检测 | `_on_speech_begin` 回调 | 语音优先，即将产出 SpeechEvent |
| 2s 轮询（用户已说话） | `wait_for_input()` 内 | 语音优先，不再需要手势 |

### 7.5 系统事件过滤

为避免系统事件和动作事件重复处理同一事件，`_filter_system_events()` 会丢弃与 `GESTURE_MAPPING` 键名（13 种动作类型）重名的系统事件类型。例如，外部服务同时推送了 `{"event": "nod", ...}` 到系统事件端口和 `{"action_type": "nod", ...}` 到动作事件端口时，系统事件管道中的会被丢弃，统一走动作事件管道处理。

---

## 八、外部服务依赖

| 服务 | 端点 | 用途 |
|------|------|------|
| 声纹+人脸识别服务 | `192.168.217.100:8001` | 用户身份识别（声纹搜索 + 人脸检测） |
| Shell 代理服务器 | `192.168.217.100:8088` | 机器人命令远程执行 |
| Foxglove WebSocket Bridge | `192.168.217.253:8768` | ROS2 摄像头视频流订阅 |
| 机器人控制器 | `192.168.217.253:9092` | 机器人物理动作 WebSocket |
| SystemEventServer | `0.0.0.0:8769` | 系统事件 HTTP 接收（`SYSTEM_EVENT_PORT`） |
| ActionEventServer | `0.0.0.0:8770` | 动作事件 HTTP 接收（`ACTION_EVENT_PORT`） |
| ByteDance SeedASR | `openspeech.bytedance.com` | 云端流式语音识别 |
| ByteDance Seed-TTS | `openspeech.bytedance.com` | 云端流式语音合成 |
| DashScope (Qwen-Omni) | `dashscope.aliyuncs.com/compatible-mode/v1` | LLM 推理 + 原生音频生成 |
| Ark (豆包) | `ark.cn-beijing.volces.com/api/v3` | LLM 推理 |
| 小米 MiMo | — | LLM 推理 |
| Alibaba IQS | `cloud-iqs.aliyuncs.com` | 联网搜索 + 天气查询 |

---

## 九、AI 模型汇总

| 模型 | 用途 | 所属层 | 部署方式 |
|------|------|--------|---------|
| sherpa-onnx KWS zipformer | 唤醒词检测（"小伴小伴"） | 感知层 | 本地离线 |
| sherpa-onnx zipformer 双语 | 本地语音识别（ASR） | 感知层 | 本地离线 |
| ByteDance SeedASR 2.0 | 云端语音识别（ASR） | 感知层 | 云端 API |
| 声纹识别模型 | 说话人身份识别 | 感知层 | 远程服务 |
| 人脸识别模型 | 画面人物身份识别 | 感知层 | 远程服务 |
| doubao-seed-2-0-lite-260428 | 主力 LLM（大模型，强推理） | 决策层 | 云端 API（Ark） |
| doubao-seed-2-0-mini-260428 | 主力 LLM（小模型，低延迟） | 决策层 | 云端 API（Ark） |
| qwen3.5-omni-plus | 主力 LLM（大模型，原生音频输出） | 决策层 | 云端 API（DashScope） |
| qwen3.5-omni-flash | 主力 LLM（小模型，原生音频输出） | 决策层 | 云端 API（DashScope） |
| mimo-v2.5 | 主力 LLM（小米 MiMo） | 决策层 | 云端 API（小米） |
| qwen3.6-35b-a3b | 对话摘要压缩 + 路由器回退 | 决策层 | 云端 API（DashScope） |
| MediaPipe Face Landmarker + Gesture Recognizer | 手势动作检测（13 种手势，本地离线推理） | 感知层 | 本地离线 → HTTP 推送至 ActionEventServer |
| 外部视觉检测服务 | 动作事件推送（nod/shake/wave/疼痛/手势等） | 感知层 | HTTP → ActionEventServer (端口 8770) |
| ByteDance Seed-TTS 2.0 | 语音合成（TTS） | 执行层 | 云端 API |

**模型选择策略**：

```
主力 LLM 选择:
  LLM_PROVIDER="doubao"   → doubao-seed-2-0-lite/mini (文本输出 + 外部 TTS)
  LLM_PROVIDER="qwen_omni" → qwen3.5-omni-plus/flash (文本+原生音频输出)
  LLM_PROVIDER="xiaomi"   → mimo-v2.5 (文本输出 + 外部 TTS)

大/小模型切换:
  model_size="large"  → 物理操作（喂饭/喂水/康复运动）、工具链、手势交互
  model_size="small" → 普通闲聊、简单问答

ASR 选择:
  ASR_PROVIDER="cloud"       → ByteDance SeedASR 2.0
  ASR_PROVIDER="sherpa_onnx"  → sherpa-onnx 本地模型
```

---

## 十、后续优化方向

### 1. 提高动作/行为检测准确性

**现状**：手势检测采用 MediaPipe Face Landmarker + Gesture Recognizer 本地离线方案，已支持点头/摇头/歪头/挥手/举手及 7 种静态手势。存在以下可优化空间：
- 时序特征利用有限，可进一步利用连续帧的运动轨迹提升准确率
- 未融合机器人本体传感器数据（关节角度、加速度等），缺乏多模态行为理解能力

**优化方向**：引入时序动作检测模型，利用连续视频帧捕捉动作动态；扩展养老场景关键行为类型（求助、指向、起身等）；融合机器人本体传感器数据，实现多模态行为理解。

### 2. 调研 Omni 和具身大模型，提升复杂决策与执行能力

#### 2.1 全模态模型部署与测试

当前系统已接入 Qwen-Omni 和豆包，但视觉理解和原生多模态能力尚未充分挖掘。计划调研并部署主流全模态模型，横向对比多模态理解、语音质量、工具调用、延迟和成本等维度，评估大规模部署可行性。

#### 2.2 具身规划模型调研

调研端到端具身规划模型，探索将现有技能系统升级为可执行技能图，并研究端到端感知-规划-执行方案。

### 3. 增加问答知识库功能

当前 Agent 依赖 LLM 内置知识和联网搜索，缺少养老护理领域专属知识。计划引入 RAG 架构搭建本地向量知识库，覆盖养老护理知识、机器人使用指南、养老院专属信息等，并支持护工/管理员自助更新维护。

### 4. 增加长期记忆

当前系统仅通过 LangGraph MemorySaver 保存对话历史，缺乏对用户长期信息的持久化存储。计划引入用户画像系统，结构化存储用户基础属性、偏好习惯、健康轨迹和社交关系，支持跨会话记忆检索和对话后自动提取关键信息增量更新。

### 5. 扩展外部系统事件类型与检测能力

当前 `SystemEventServer` 仅接收通用 JSON 事件，无事件分类和优先级机制。计划建立事件类型体系（安全告警/环境提醒/设备状态/日程提醒），支持优先级分级处理和多协议接入（MQTT、WebSocket），并引入事件去重、防抖与溯源审计能力。
