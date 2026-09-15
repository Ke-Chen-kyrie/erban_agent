# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这是什么

`sing` —— 豆包实时语音模型 3.0（Seeduplex）唱歌工具。输入一句唱歌需求描述，模型现场唱一段、直接通过扬声器播放，并返回歌词摘要。核心逻辑全部在 `sing_tool.py` 一个文件里（`SingTool` 类 + Linux 风格 CLI），其余文件是配置、打包和预生成的音效。

## 常用命令

```bash
# 安装（在 local_cli/ 目录下执行：生成 local_cli/.venv 里的 sing 命令；系统需先装 portaudio19-dev）
python3 -m venv .venv && .venv/bin/pip install -e ./sing

# 运行（需先激活环境或用虚拟环境里的二进制）
.venv/bin/sing "唱一首温柔甜美的生日歌"
.venv/bin/sing My Heart Will Go On           # 英文歌名含空格可不加引号（多个词自动拼接）
.venv/bin/sing --no-play "唱一首歌"          # 只返回摘要，不播放（干跑，最快验证）
.venv/bin/sing --no-music "念一段"           # 关唱歌，纯念白
.venv/bin/sing -v <音色ID> "唱一首新年歌"     # 覆盖音色
.venv/bin/sing --make-trigger --trigger-phrase "请开始吧"   # 重新合成起拍语音
.venv/bin/sing --help
```

没有测试套件、没有 lint/格式化配置。最快的验证路径是 `--no-play`（跳过扬声器，只走一遍网络和模型流程）。部署到新电脑、卸载、常见问题等详见 `README.md`。

## 核心机制（读多文件才能看懂的部分）

「让模型主动唱歌」靠的是 Seeduplex 双工协议里的两个套路，都在 `_sing_async` / `_sing_session_payload` 里：

1. **把需求写进系统提示词**：唱歌需求被塞进 `session.instructions`（`_sing_session_payload` 的 `instructions`，规则为「会唱就直接唱；不会唱就先直接说『不会唱这首歌』，再推荐并演唱一首会唱的」，末尾接「现在请唱：{requirement}。」），同时开启 `extension.extra.enable_proactive_speak`。
2. **喂回起拍语音触发**：预热时不发用户音频，而是把预合成的 `trigger.wav`（默认"开始唱吧"）按 `FILE_CHUNK` 分片作为 `input_audio_buffer.append` 喂回，之后 `commit` + `input_audio_mute.commit`，模型即主动开唱。

演唱本身由 `extension.dialog.extra.enable_music` 开关控制（`--no-music` 时关掉，变成念白）。

`trigger.wav` 不是独立的 TTS 服务产物，而是用同一模型的 TTS 通道合成：`make_trigger()` → `_tts()` 走 `session.create`（不带 instructions）+ `speech_text_buffer.commit`，拿到 24k PCM 后重采样到 16k 写盘。

数据流：文本需求 → 系统提示词 + 起拍语音 → WebSocket 推流 → 收集 `response.output_text.delta` / `response.output_audio.delta` → 扬声器播放 → 返回 `{ok, text, summary, voice, duration_ms, audio_bytes, usage}`。

## 协议与音频常量（`sing_tool.py` 顶部）

- 端点：`wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue`，模型 `MODEL = "1.2.6.1"`。
- 鉴权都在 WebSocket header 上：`X-Api-App-Key` / `X-Api-App-ID` / `X-Api-Access-Key` / `X-Api-Resource-Id`（见 `_connect`）。
- 采样率：输入（起拍）`INPUT_RATE = 16000`，模型输出 `OUTPUT_RATE = 24000`，均为 int16 单声道 PCM。播放时以设备原生采样率（`default_samplerate`）开流、把 24k 源经 `resample_poly` 重采样到设备率。
- `FILE_CHUNK = 640` 字节 = 16k int16 下单帧 20ms，推流时每片 `sleep(0.02)` 模拟实时。
- `_resample_s16le` 用 scipy `resample_poly`（float32 精度）做 int16 重采样（如起拍语音 24k→16k）；扬声器播放走 `sounddevice`（dtype=float32），设备选择 / 采样率与 proactive_agent_realtime 的 `AudioPlayer` 一致。

## 配置（`config.toml`）

`config.toml`、`trigger.wav` 的默认路径固定解析到 `sing_tool.py` 所在目录（与调用时的 cwd 无关，靠 `DEFAULT_CONFIG_PATH` / `TRIGGER_FILE`）。

`config.toml` 分三段，`_load_cfg` 读取并合并默认值：

- `[auth]`：`api_key`（Access Token）、`app_id`、`app_key`（固定公共网关 key）、`resource_id`。
- `[session]`：`speaker`（音色 ID）、`enable_music`。
- `[device]`：`speaker_name`（扬声器名称关键词，留空=系统默认）。

扬声器选择有两处环境变量覆盖：`SPEAKER_DEVICE_NAME`、`SPEAKER_DEVICE_INDEX`（优先级高于 toml，见 `_load_cfg`）。

⚠️ `config.toml` 里是真实的鉴权密钥，不能提交到公共仓库，也不要回显到日志/输出。

## 对外接口（作为库使用）

`sing_tool.py` 同时是可嵌入的库，除了 `main()` 入口（`pyproject.toml` 的 `sing = "sing_tool:main"`）还导出：

- `SingTool(config_path)` → `.sing()` / `.sing_detailed()`
- 模块级 `sing()` / `sing_detailed()` / `make_trigger()`
- `TOOL_SCHEMA`：agent function-calling 的工具描述，供调用方把「唱歌」包装成 agent 工具。

## Python 版本

`pyproject.toml` 声明 `requires-python = ">=3.10"`（与 proactive_agent_realtime 一致），用 `tomllib`（3.11+）否则回退 `tomli` 兼容旧版本。播放依赖已从 `pyaudio` 改为 `sounddevice` / `numpy` / `scipy`。