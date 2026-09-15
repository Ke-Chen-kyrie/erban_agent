# sing —— 豆包实时语音模型 3.0（Seeduplex）唱歌工具

输入一句唱歌需求描述，模型现场唱一段、直接通过扬声器播放，并返回歌词摘要。核心逻辑全部在 `sing_tool.py` 一个文件里（`SingTool` 类 + Linux 风格 CLI），其余文件是配置、打包和预生成的音效。

## 依赖与系统要求

- Python ≥ 3.10
- 系统需装 PortAudio：`sudo apt install portaudio19-dev`
- Python 依赖见 `pyproject.toml` / `requirements.txt`：`websockets`、`sounddevice`、`numpy`、`scipy`

## 安装

`.venv` 固定放在 `local_cli/` 目录下（供上层 `proactive_agent_realtime` 用 `local_cli/.venv/bin/sing` 调用），因此在 `local_cli/` 下执行：

```bash
cd local_cli
python3 -m venv .venv
.venv/bin/pip install -e ./sing
```

装完后生成 `local_cli/.venv/bin/sing` 命令。

## 使用

```bash
.venv/bin/sing "唱一首温柔甜美的生日歌"
.venv/bin/sing My Heart Will Go On           # 英文歌名含空格，可不加引号（多个词自动拼接）
.venv/bin/sing --no-play "唱一首歌"          # 只返回摘要，不播放（干跑，最快验证）
.venv/bin/sing --no-music "念一段"           # 关唱歌，纯念白
.venv/bin/sing -v <音色ID> "唱一首新年歌"     # 覆盖音色
.venv/bin/sing --make-trigger --trigger-phrase "请开始吧"   # 重新合成起拍语音
.venv/bin/sing --help
```

`config.toml`、`trigger.wav` 的默认路径固定解析到 `sing_tool.py` 所在目录，与调用时所在目录（cwd）无关。

## 工作原理（简版）

1. 把唱歌需求写进 `session.instructions`，并开启 `extension.extra.enable_proactive_speak`。
2. 把预合成的 `trigger.wav`（起拍语音）作为输入音频喂回，触发模型主动开唱。
3. 演唱由 `extension.dialog.extra.enable_music` 控制（`--no-music` 时变纯念白）。

`trigger.wav` 用同一模型的 TTS 通道合成（`make_trigger()`），24k PCM 重采样到 16k 写盘。

## 配置

见 `config.toml`，分三段：

- `[auth]`：`api_key`（Access Token）、`app_id`、`app_key`、`resource_id`
- `[session]`：`speaker`（音色 ID）、`enable_music`
- `[device]`：`speaker_name`（扬声器名称关键词，留空=系统默认）

⚠️ `config.toml` 里是真实鉴权密钥，勿提交公共仓库、勿回显到日志/输出。

## 扬声器选择与播放

- 优先级：名称关键词 > 显式索引 > 系统默认；环境变量 `SPEAKER_DEVICE_NAME` / `SPEAKER_DEVICE_INDEX` 优先级高于 toml。
- 播放走 `sounddevice`：以设备原生采样率（`default_samplerate`）开流，把模型 24k 输出经 `resample_poly` 重采样到设备率，dtype 为 float32（与 `proactive_agent_realtime` 的 `AudioPlayer` 一致）。

## 作为库使用

```python
from sing_tool import SingTool, sing, sing_detailed, make_trigger, TOOL_SCHEMA

SingTool().sing("唱一首温柔的生日歌")   # 返回摘要字符串
SingTool().sing_detailed("唱一首歌")     # 返回 {ok, text, summary, voice, duration_ms, audio_bytes, usage}
```

`TOOL_SCHEMA` 是 agent function-calling 的工具描述，可把「唱歌」包装成 agent 工具。

## 常见问题

- **找不到扬声器 / 无声音**：核对 `[device] speaker_name` 关键词是否命中下面命令列出的设备名：
  `python -c "import sounddevice as sd; print(sd.query_devices())"`。
- **`--no-play` 跑通但播放失败/无声**：确认声卡没被其它进程占用；确保已装 `portaudio19-dev`；本工具已用 float32 适配 USB 声卡。
- **卸载**：删除 `local_cli/.venv`，或 `.venv/bin/pip uninstall seeduplex-sing`。