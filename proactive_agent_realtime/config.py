import os
from dotenv import load_dotenv

load_dotenv()

WAKEUP_KEYWORD = os.getenv("WAKEUP_KEYWORD", "小伴小伴")

# ─── 打断检测配置 ───
INTERRUPT_KEYWORD = os.getenv("INTERRUPT_KEYWORD", WAKEUP_KEYWORD)  # 打断词默认与唤醒词一致
# INTERRUPT_MODE: "vad" = 服务端语义 VAD 自动打断（现状）；"keyword" = 本地 KWS 只认打断词打断
INTERRUPT_MODE = os.getenv("INTERRUPT_MODE", "vad")

# ─── 唤醒检测配置 ───
WAKEUP_MODEL_DIR = os.getenv("WAKEUP_MODEL_DIR", "models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20")
WAKEUP_COOLDOWN_SECONDS = float(os.getenv("WAKEUP_COOLDOWN_SECONDS", "3"))

# ─── 音频设备配置 ───
# 优先按名称匹配（重启后索引可能变化），None 表示使用系统默认设备
MIC_DEVICE_NAME = os.getenv("MIC_DEVICE_NAME", "Wireless Mic Rx")
SPEAKER_DEVICE_NAME = os.getenv("SPEAKER_DEVICE_NAME", "USB Audio Device")
MIC_DEVICE_INDEX = None
SPEAKER_DEVICE_INDEX = None

# ─── 远讲抑制（噪声门）───
# RMS 低于阈值的音频 chunk 平滑淡出（只收近处人声）。实测：近讲 0.02~0.09，远讲 0.00x。
NOISE_GATE_ENABLED = os.getenv("NOISE_GATE_ENABLED", "false").lower() in ("true", "1", "yes")
NOISE_GATE_THRESHOLD = float(os.getenv("NOISE_GATE_THRESHOLD", "0.014"))    # P 开门阈值（RMS 线性）
NOISE_GATE_ONSET_DELTA = float(os.getenv("NOISE_GATE_ONSET_DELTA", "0.0"))  # D 陡升预开，0=关闭
NOISE_GATE_RELEASE_ALPHA = float(os.getenv("NOISE_GATE_RELEASE_ALPHA", "0.3"))   # I 慢关回落系数（0~1，越大关得越慢）
NOISE_GATE_CLOSE_RATIO = float(os.getenv("NOISE_GATE_CLOSE_RATIO", "0.85"))      # 回滞：包络低于阈值×此值才关门

# ─── 声纹+人脸识别服务 ───
IDENTIFICATION_URL = os.getenv("IDENTIFICATION_URL", "http://192.168.217.100:8001")

# ─── 执行工具服务器配置 ───
SHELL_PROXY_URL = os.getenv("SHELL_PROXY_URL", "http://192.168.217.100:8088/run")

# ─── 摄像头配置 ───
CAMERA_SOURCE = os.getenv("CAMERA_SOURCE", "local")  # "local" | "foxglove" | "zenoh"
LOCAL_CAMERA_INDEX = int(os.getenv("LOCAL_CAMERA_INDEX", "0"))
LOCAL_CAMERA_WIDTH = int(os.getenv("LOCAL_CAMERA_WIDTH", "1280"))
LOCAL_CAMERA_HEIGHT = int(os.getenv("LOCAL_CAMERA_HEIGHT", "720"))
FOXGLOVE_BRIDGE_URL = os.getenv("FOXGLOVE_BRIDGE_URL", "ws://192.168.217.253:8768")
CAMERA_TOPIC_HEAD = os.getenv("CAMERA_TOPIC_HEAD", "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed")
ZENOH_TOPIC = os.getenv("ZENOH_TOPIC", "camera/annotated")
ZENOH_URL = os.getenv("ZENOH_URL", "")  # 留空 = 局域网 multicast 自动发现
CAMERA_FPS = float(os.getenv("CAMERA_FPS", "5.0"))

DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")

# ─── DashScope Workspace ───
DASHSCOPE_WORKSPACE_ID = os.getenv("DASHSCOPE_WORKSPACE_ID", "")

# ─── 日志配置 ───
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG")  # "DEBUG" 或 "INFO"
LOG_FILE = os.getenv("LOG_FILE", "")         # 日志文件路径，空字符串 = 仅控制台

# ─── Qwen-Omni-Realtime 配置 ───
QWEN_OMNI_MODEL = os.getenv("QWEN_OMNI_MODEL", "qwen3.5-omni-plus-realtime")
QWEN_OMNI_REGION = os.getenv("QWEN_OMNI_REGION", "cn-beijing")

# ─── Omni 原生语音输出配置 ───
OMNI_VOICE = os.getenv("OMNI_VOICE", "Tina")
OMNI_SAMPLE_RATE = int(os.getenv("OMNI_SAMPLE_RATE", "24000"))

# ─── 对话控制 ───
SENTENCE_TIMEOUT = float(os.getenv("SENTENCE_TIMEOUT", "20.0"))         # 双方沉默超时秒数

# ─── Shell 代理配置 ───
SHELL_PROXY_TIMEOUT = int(os.getenv("SHELL_PROXY_TIMEOUT", "180"))      # HTTP 请求超时秒数

# ─── 音频播放配置 ───
AUDIO_SOURCES = os.getenv("AUDIO_SOURCES", "omni")  # "omni" | "tts"
BYTEDANCE_TTS_SAMPLE_RATE = int(os.getenv("BYTEDANCE_TTS_SAMPLE_RATE", "24000"))
AUDIO_DRAIN_SILENCE_MS = int(os.getenv("AUDIO_DRAIN_SILENCE_MS", "800"))  # drain 静音长度 ms，需大于声卡硬件缓冲区

# ─── 本地 CLI（local_cli 独立项目）配置 ───
# 与 execute_shell 配合：命令首词命中本地命令白名单时，在本机 local_cli 环境执行；否则走远程 shell 代理
_DEFAULT_LOCAL_CLI_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "local_cli"
)
LOCAL_CLI_DIR = os.getenv("LOCAL_CLI_DIR", _DEFAULT_LOCAL_CLI_DIR)
# 本地命令白名单（逗号分隔）。local_cli 里新增命令后，把命令名加到这里即可
LOCAL_CLI_COMMANDS = tuple(
    c.strip() for c in os.getenv("LOCAL_CLI_COMMANDS", "sing").split(",") if c.strip()
)
LOCAL_CLI_TIMEOUT = int(os.getenv("LOCAL_CLI_TIMEOUT", "300"))  # 本地命令最长等待秒数

# ─── 统一事件配置 ───
ACTION_EVENT_ENABLED = os.getenv("ACTION_EVENT_ENABLED", "true").lower() != "false"
EVENT_PORT = int(os.getenv("EVENT_PORT", "8769"))
EVENT_SERVER_ENABLED = os.getenv("EVENT_SERVER_ENABLED", "true").lower() != "false"

# ─── 大屏渲染 WebSocket ──────────────────────────────────
RENDER_SOCKET_URL = os.getenv("RENDER_SOCKET_URL", "ws://localhost:8770/render")
RENDER_SOCKET_ENABLED = os.getenv("RENDER_SOCKET_ENABLED", "false").lower() in ("true", "1", "yes")

# ─── 启动时校验 ───

_VALID_OMNI_VOICES = {
    "Serena", "Theo Calm", "Harvey", "Mia", "Kiki", "Sunny",
    "Tina", "Ethan", "Andre", "Maia", "Liora Mira",
}


def validate_config():
    """启动时校验关键配置，错误时抛出 ValueError 并给出明确提示。"""
    errors = []

    if not DASHSCOPE_API_KEY:
        errors.append("DASHSCOPE_API_KEY 未设置")

    if OMNI_VOICE not in _VALID_OMNI_VOICES:
        errors.append(f"OMNI_VOICE={OMNI_VOICE!r}，有效值: {sorted(_VALID_OMNI_VOICES)}")

    if CAMERA_SOURCE not in ("local", "foxglove", "zenoh"):
        errors.append(f"CAMERA_SOURCE={CAMERA_SOURCE!r}，有效值: local, foxglove, zenoh")

    if INTERRUPT_MODE not in ("vad", "keyword"):
        errors.append(f"INTERRUPT_MODE={INTERRUPT_MODE!r}，有效值: vad, keyword")

    if errors:
        raise ValueError("配置校验失败:\n  " + "\n  ".join(errors))
