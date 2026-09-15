import os
from dotenv import load_dotenv

load_dotenv()

# ─── 摄像头配置 ───
CAMERA_SOURCE = os.getenv("CAMERA_SOURCE", "local")  # "foxglove"（ROS2）或 "local"（cv2.VideoCapture）
CAMERA_FPS = float(os.getenv("CAMERA_FPS", "10.0"))
CAMERA_TOPIC_HEAD = os.getenv("CAMERA_TOPIC_HEAD", "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed")
FOXGLOVE_BRIDGE_URL = os.getenv("FOXGLOVE_BRIDGE_URL", "ws://192.168.217.253:8768")

# ─── 视频压缩 ───
_VIDEO_W = int(os.getenv("VIDEO_COMPRESS_WIDTH", "768"))
_VIDEO_H = int(os.getenv("VIDEO_COMPRESS_HEIGHT", "576"))
VIDEO_COMPRESS_TARGET_SIZE = (_VIDEO_W, _VIDEO_H)

# ─── 音频设备 ───
MIC_DEVICE_NAME = os.getenv("MIC_DEVICE_NAME", "USB Audio")

# ─── MiniCPM-o Realtime API ───
MINICPMO_HOST = os.getenv("MINICPMO_HOST", "https://180.76.143.172:8266/")
MINICPMO_MODE = os.getenv("MINICPMO_MODE", "video")
MINICPMO_SEND_INTERVAL = float(os.getenv("MINICPMO_SEND_INTERVAL", "1.0"))
MINICPMO_SESSION_DURATION = float(os.getenv("MINICPMO_SESSION_DURATION", "180"))

# ─── 人脸识别 ───
IDENT_API_BASE = os.getenv("IDENT_API_BASE", "http://localhost:8001")
FACE_RECOGNITION_THRESHOLD = float(os.getenv("FACE_RECOGNITION_THRESHOLD", "0.7"))

# ─── 日志 ───
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "")


def validate_config():
    """启动时校验关键配置。"""
    pass