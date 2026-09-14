import os
from dotenv import load_dotenv

load_dotenv()

# ─── 火山引擎 SeedASR 2.0（豆包流式语音识别模型2.0）───
BYTEDANCE_API_KEY = os.getenv("BYTEDANCE_API_KEY", "")
BYTEDANCE_APP_KEY = os.getenv("BYTEDANCE_APP_KEY", "")
BYTEDANCE_ACCESS_KEY = os.getenv("BYTEDANCE_ACCESS_KEY", "")
BYTEDANCE_RESOURCE_ID = os.getenv("BYTEDANCE_RESOURCE_ID", "volc.seedasr.sauc.duration")
BYTEDANCE_ASR_URL = os.getenv(
    "BYTEDANCE_ASR_URL",
    "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async",
)

WAKEUP_KEYWORD = os.getenv("WAKEUP_KEYWORD", "小伴小伴")

# ─── 唤醒检测配置 ───
WAKEUP_MODEL_DIR = os.getenv("WAKEUP_MODEL_DIR", "models/sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20")
WAKEUP_COOLDOWN_SECONDS = float(os.getenv("WAKEUP_COOLDOWN_SECONDS", "3"))
WAKEUP_BUFFER_DURATION = float(os.getenv("WAKEUP_BUFFER_DURATION", "3.0"))

# ─── 音频设备配置 ───
# 优先按名称匹配（重启后索引可能变化），None 表示使用系统默认设备
# MIC_DEVICE_NAME = os.getenv("MIC_DEVICE_NAME", "Wireless Mic Rx")
MIC_DEVICE_NAME = os.getenv("MIC_DEVICE_NAME", "USB Composite Device")
#SPEAKER_DEVICE_NAME = os.getenv("SPEAKER_DEVICE_NAME", "YUNJI")
SPEAKER_DEVICE_NAME = os.getenv("SPEAKER_DEVICE_NAME", "YUNJI")
MIC_DEVICE_INDEX = None
SPEAKER_DEVICE_INDEX = None

# ─── 声纹+人脸识别服务 ───
IDENTIFICATION_URL = os.getenv("IDENTIFICATION_URL", "http://192.168.217.100:8001")

# ─── SKEL 关节推理服务 ───
JOINT_SERVICE_URL = os.getenv("JOINT_SERVICE_URL", "http://192.168.217.100:8002")
JOINT_SERVICE_TIMEOUT = int(os.getenv("JOINT_SERVICE_TIMEOUT", "10"))

# ─── 执行工具服务器配置 ───
SHELL_PROXY_URL = os.getenv("SHELL_PROXY_URL", "http://192.168.217.100:8088/run")

# ─── 相机配置 ───
CAMERA_SOURCE = os.getenv("CAMERA_SOURCE", "foxglove")  # "foxglove"（ROS2）或 "local"（cv2.VideoCapture）
LOCAL_CAMERA_INDEX = int(os.getenv("LOCAL_CAMERA_INDEX", "0"))  # 本地摄像头 /dev/videoX 索引
LOCAL_CAMERA_WIDTH = int(os.getenv("LOCAL_CAMERA_WIDTH", "1280"))
LOCAL_CAMERA_HEIGHT = int(os.getenv("LOCAL_CAMERA_HEIGHT", "720"))
FOXGLOVE_BRIDGE_URL = os.getenv("FOXGLOVE_BRIDGE_URL", "ws://192.168.217.253:8768")
CAMERA_TOPIC_HEAD = os.getenv("CAMERA_TOPIC_HEAD", "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed")
CAMERA_FPS = float(os.getenv("CAMERA_FPS", "10.0"))

# ─── 语音打断检测参数 ───
INTERRUPT_ENERGY_THRESHOLD = float(os.getenv("INTERRUPT_ENERGY_THRESHOLD", "0.8"))
INTERRUPT_WINDOW_SIZE = int(os.getenv("INTERRUPT_WINDOW_SIZE", "20"))
INTERRUPT_MIN_SPEECH_FRAMES = int(os.getenv("INTERRUPT_MIN_SPEECH_FRAMES", "3"))
INTERRUPT_FRAME_MS = float(os.getenv("INTERRUPT_FRAME_MS", "80.0"))
INTERRUPT_MIC_SAMPLE_RATE = int(os.getenv("INTERRUPT_MIC_SAMPLE_RATE", "16000"))

# ─── 日志配置 ───
LOG_LEVEL = os.getenv("LOG_LEVEL", "DEBUG")  # "DEBUG" 或 "INFO"
LOG_FILE = os.getenv("LOG_FILE", "")         # 日志文件路径，空字符串 = 仅控制台

# ─── 声纹识别阈值（低于此分数视为路人）───
VOICEPRINT_THRESHOLD = float(os.getenv("VOICEPRINT_THRESHOLD", "0.4"))

# ─── 视频处理参数 ───
VIDEO_COMPRESS_TARGET_SIZE = (1280,720)
VIDEO_FPS_FALLBACK = float(os.getenv("VIDEO_FPS_FALLBACK", "10.0"))
VIDEO_FOURCC = os.getenv("VIDEO_FOURCC", "MJPG")
VIDEO_KEY_FRAME_MAX = int(os.getenv("VIDEO_KEY_FRAME_MAX", "15"))

QWEN_LLM_CONFIG = {
    "model": "qwen3.5-omni-plus",
    "api_key": os.getenv("DASHSCOPE_API_KEY", ""),
    "base_url": os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    "streaming": True,
    "model_kwargs": {"enable_thinking": False},
}

# ─── 意图判断小模型）───
SMALL_INTENT_LLM_CONFIG = {
    "model": "qwen3.6-35b-a3b",
    "api_key": os.getenv("DASHSCOPE_API_KEY", ""),
    "base_url": os.getenv("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
}


# ─── 豆包 LLM（Ark API）───
BYTEDANCE_LLM_CONFIG = {
    "model": "doubao-seed-2-0-lite-260428",
    "api_key": os.getenv("ARK_API_KEY", ""),
    "base_url": os.getenv("BYTEDANCE_LLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
}


# ─── 小米 MiMo LLM ───
MIMO_API_KEY = os.getenv("MIMO_API_KEY", "")
MIMO_LLM_CONFIG = {
    "model": "mimo-v2.5",
    "api_key": MIMO_API_KEY,
    "base_url": os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1"),
}


# ─── Omni 原生语音输出配置 ───
OMNI_VOICE = os.getenv("OMNI_VOICE", "Tina")
OMNI_AUDIO_FORMAT = os.getenv("OMNI_AUDIO_FORMAT", "wav")
OMNI_SAMPLE_RATE = int(os.getenv("OMNI_SAMPLE_RATE", "24000"))

# LLM 提供商: "doubao" | "qwen_omni" | "xiaomi"
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "doubao")

# 音频来源: "omni" (Omni 原生音频) | "tts" (ByteDance TTS)
AUDIO_SOURCES = os.getenv("AUDIO_SOURCES", "tts")

# ─── ByteDance TTS 配置 (AUDIO_SOURCES="tts" 时生效) ───
BYTEDANCE_TTS_URL = os.getenv("BYTEDANCE_TTS_URL", "wss://openspeech.bytedance.com/api/v3/tts/bidirection")
BYTEDANCE_TTS_API_KEY = os.getenv("BYTEDANCE_TTS_API_KEY", BYTEDANCE_API_KEY)
BYTEDANCE_TTS_RESOURCE_ID = os.getenv("BYTEDANCE_TTS_RESOURCE_ID", "seed-tts-2.0")
BYTEDANCE_TTS_SPEAKER = os.getenv("BYTEDANCE_TTS_SPEAKER", "saturn_zh_female_qingyingduoduo_cs_tob")
BYTEDANCE_TTS_SAMPLE_RATE = int(os.getenv("BYTEDANCE_TTS_SAMPLE_RATE", "24000"))
BYTEDANCE_TTS_SPEECH_RATE = int(os.getenv("BYTEDANCE_TTS_SPEECH_RATE", "0"))

# ─── 历史消息保留轮数（超过后压缩旧消息为摘要）───
HISTORY_KEEP_ROUNDS = int(os.getenv("HISTORY_KEEP_ROUNDS", "15"))

# ─── ASR 配置 ───
# ASR 提供商: "cloud" (ByteDance SeedASR 云端) | "sherpa_onnx" (sherpa-onnx 本地)
ASR_PROVIDER = os.getenv("ASR_PROVIDER", "cloud")
ASR_END_WINDOW_SIZE = int(os.getenv("ASR_END_WINDOW_SIZE", "500"))      # VAD 静音窗口 ms
ASR_KEEPALIVE_INTERVAL = float(os.getenv("ASR_KEEPALIVE_INTERVAL", "5"))  # 暂停时静音保活间隔 s

# ─── sherpa-onnx 本地 ASR 配置 (ASR_PROVIDER="sherpa_onnx" 时生效) ───
SHERPA_ONNX_MODEL_DIR = os.getenv("SHERPA_ONNX_MODEL_DIR", "models/sherpa-onnx-streaming-zipformer-bilingual-zh-en")

# ─── 对话控制 ───
SENTENCE_TIMEOUT = float(os.getenv("SENTENCE_TIMEOUT", "20.0"))         # 无语音超时秒数
AGENT_MAX_RETRIES = int(os.getenv("AGENT_MAX_RETRIES", "2"))            # LLM 调用最大重试次数
AGENT_RETRY_DELAYS = os.getenv("AGENT_RETRY_DELAYS", "1.0,2.0")         # 重试间隔（逗号分隔）

# ─── 环境观察工具参数 ───
OBSERVE_MAX_DURATION = int(os.getenv("OBSERVE_MAX_DURATION", "30"))     # 视频最长秒数
OBSERVE_VIDEO_FPS = float(os.getenv("OBSERVE_VIDEO_FPS", "5"))       # 视频录制帧率
OBSERVE_SAMPLE_RATE = int(os.getenv("OBSERVE_SAMPLE_RATE", "16000"))    # 音频录制采样率
FACE_RECOGNITION_THRESHOLD = float(os.getenv("FACE_RECOGNITION_THRESHOLD", "0.75"))  # 人脸识别置信度阈值（低于此分数视为路人）

# ─── Shell 代理配置 ───
SHELL_PROXY_TIMEOUT = int(os.getenv("SHELL_PROXY_TIMEOUT", "180"))      # HTTP 请求超时秒数

# ─── 音频播放配置 ───
AUDIO_DRAIN_SILENCE_MS = int(os.getenv("AUDIO_DRAIN_SILENCE_MS", "300"))  # drain 静音长度 ms

# ─── 多模态路由配置 ───
ROUTER_ENABLED = os.getenv("ROUTER_ENABLED", "true").lower() != "false"
ROUTER_VISUAL_ENABLED = os.getenv("ROUTER_VISUAL_ENABLED", "true").lower() != "false"
ROUTER_VISUAL_FALLBACK_ENABLED = os.getenv("ROUTER_VISUAL_FALLBACK_ENABLED", "false").lower() != "false"

# ─── 统一事件配置 ───
ACTION_EVENT_ENABLED = os.getenv("ACTION_EVENT_ENABLED", "true").lower() != "false"
EVENT_PORT = int(os.getenv("EVENT_PORT", "8769"))
EVENT_SERVER_ENABLED = os.getenv("EVENT_SERVER_ENABLED", "true").lower() != "false"

# ─── 调试配置 ───
DEBUG_SAVE_MEDIA = os.getenv("DEBUG_SAVE_MEDIA", "false").lower() != "false"


# ─── 启动时校验 ───

_VALID_LLM_PROVIDERS = {"doubao", "qwen_omni", "xiaomi"}
_VALID_ASR_PROVIDERS = {"cloud", "sherpa_onnx"}
_VALID_AUDIO_SOURCES = {"tts", "omni"}
_VALID_OMNI_VOICES = {
    "Serena", "Theo Calm", "Harvey", "Mia", "Kiki", "Sunny",
    "Tina", "Ethan", "Andre", "Maia", "Liora Mira",
}


def validate_config():
    """启动时校验关键配置，错误时抛出 ValueError 并给出明确提示。"""
    errors = []

    if LLM_PROVIDER not in _VALID_LLM_PROVIDERS:
        errors.append(f"LLM_PROVIDER={LLM_PROVIDER!r}，有效值: {_VALID_LLM_PROVIDERS}")

    if LLM_PROVIDER == "doubao" and not BYTEDANCE_LLM_CONFIG["api_key"]:
        errors.append("LLM_PROVIDER=doubao 需要设置 ARK_API_KEY")
    if LLM_PROVIDER == "qwen_omni" and not QWEN_LLM_CONFIG["api_key"]:
        errors.append("LLM_PROVIDER=qwen_omni 需要设置 DASHSCOPE_API_KEY")
    if LLM_PROVIDER == "xiaomi" and not MIMO_LLM_CONFIG["api_key"]:
        errors.append("LLM_PROVIDER=xiaomi 需要设置 MIMO_API_KEY")

    if ASR_PROVIDER not in _VALID_ASR_PROVIDERS:
        errors.append(f"ASR_PROVIDER={ASR_PROVIDER!r}，有效值: {_VALID_ASR_PROVIDERS}")

    if AUDIO_SOURCES not in _VALID_AUDIO_SOURCES:
        errors.append(f"AUDIO_SOURCES={AUDIO_SOURCES!r}，有效值: {_VALID_AUDIO_SOURCES}")

    if AUDIO_SOURCES == "tts" and not BYTEDANCE_TTS_API_KEY:
        errors.append("AUDIO_SOURCES=tts 需要设置 BYTEDANCE_TTS_API_KEY 或 BYTEDANCE_API_KEY")

    if AUDIO_SOURCES != "tts" and OMNI_VOICE not in _VALID_OMNI_VOICES:
        errors.append(f"OMNI_VOICE={OMNI_VOICE!r}，有效值: {sorted(_VALID_OMNI_VOICES)}")

    if errors:
        raise ValueError("配置校验失败:\n  " + "\n  ".join(errors))

