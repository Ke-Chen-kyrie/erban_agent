"""
视频/图片编码工具 —— 将录制的视频压缩、编码为 base64，供 omni 模型使用。
"""

import base64
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from config import (
    VIDEO_COMPRESS_TARGET_SIZE,
    VIDEO_FOURCC,
    VIDEO_FPS_FALLBACK,
    VIDEO_KEY_FRAME_MAX,
)
from video.face_detect import _draw_face_detect, detect_faces
from logging_config import get_logger

logger = get_logger(__name__)


def _compute_motion_scores(frames: list) -> list:
    """计算连续帧之间的运动分数（灰度帧差均值）。
    返回 scores[i] = frames[i] 与 frames[i+1] 之间的运动量。"""
    scores = []
    for i in range(1, len(frames)):
        prev_gray = cv2.cvtColor(frames[i - 1], cv2.COLOR_BGR2GRAY)
        curr_gray = cv2.cvtColor(frames[i], cv2.COLOR_BGR2GRAY)
        diff = np.abs(curr_gray.astype(np.int16) - prev_gray.astype(np.int16))
        scores.append(float(diff.mean()))
    return scores


def _extract_peak_frames(frames: list, scores: list, top_k: int) -> list:
    """取首帧 + top-K 峰值帧 + 尾帧，按时间顺序排列，去重。"""
    if len(frames) <= 2:
        return frames
    exclude = {0, len(frames) - 1}
    indexed = [(scores[i], i + 1) for i in range(len(scores)) if (i + 1) not in exclude]
    indexed.sort(key=lambda x: x[0], reverse=True)
    peak_indices = sorted([idx for _, idx in indexed[:top_k]])
    result_indices = [0] + peak_indices + [len(frames) - 1]
    return [frames[i] for i in result_indices]


def _encode_keyframes_as_video(key_frames: list, fps: float, fourcc_str: str) -> str:
    """将已缩放的 key frames 编码为 AVI 视频（写入 /dev/shm 内存文件系统），返回 base64 字符串。"""
    height, width = key_frames[0].shape[:2]
    fourcc = cv2.VideoWriter_fourcc(*fourcc_str)

    with tempfile.NamedTemporaryFile(suffix=".avi", dir="/dev/shm", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        writer = cv2.VideoWriter(tmp_path, fourcc, fps, (width, height))
        if not writer.isOpened():
            raise RuntimeError("cv2.VideoWriter failed to open")
        for frame in key_frames:
            writer.write(frame)
        writer.release()

        with open(tmp_path, "rb") as f:
            video_bytes = f.read()
        return base64.b64encode(video_bytes).decode()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def encode_media(frames: list | None, max_key_frames: int = VIDEO_KEY_FRAME_MAX) -> tuple | None:
    """编码视频帧为 base64，对首帧和末帧并行做人脸检测。
    接受帧列表（BGR numpy arrays），运动分数计算 + 首帧人脸检测 + 末帧人脸检测三者并行，
    然后抽峰值帧 → 应用检测结果 → 统一缩放 → 编码。
    检测到人脸时用标注版替换对应帧。
    max_key_frames 控制峰值采样帧数上限，默认 15。
    返回 (b64_list, mime, type) 或 None。
    """
    if not frames:
        return None

    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            motion_future = pool.submit(_compute_motion_scores, frames)
            face_first_future = pool.submit(_draw_face_detect, frames[0])
            face_last_future = pool.submit(_draw_face_detect, frames[-1])

            scores = motion_future.result()
            key_frames = _extract_peak_frames(frames, scores, max_key_frames)
            annotated_first = face_first_future.result()
            annotated_last = face_last_future.result()

        face_detected = False
        if key_frames:
            if annotated_first is not key_frames[0]:
                key_frames[0] = annotated_first
                face_detected = True
            if annotated_last is not key_frames[-1]:
                key_frames[-1] = annotated_last
                face_detected = True

        resized_frames = [
            cv2.resize(f, VIDEO_COMPRESS_TARGET_SIZE, interpolation=cv2.INTER_AREA)
            for f in key_frames
        ]
        if not resized_frames:
            return None

        ratio = len(key_frames) / len(frames) * 100 if frames else 0
        logger.info(f"[video] 峰值采样: {len(frames)}帧 → {len(key_frames)} 关键帧 ({ratio:.0f}%) "
                     f"(首+{len(key_frames)-2}峰值+尾, 运动: {min(scores):.1f}~{max(scores):.1f})"
                     f"{' (人脸已标注)' if face_detected else ''}")

        if len(key_frames) >= 5:
            try:
                video_b64 = _encode_keyframes_as_video(
                    resized_frames, VIDEO_FPS_FALLBACK, VIDEO_FOURCC
                )
                logger.info(f"[video] 编码为视频: {len(resized_frames)} 帧, "
                             f"{len(video_b64)} 字符 base64")
                return video_b64, "video/avi", "video"
            except Exception as e:
                logger.warning(f"[video] 视频编码失败，回退为图片模式: {e}")

        b64_list = []
        for f in resized_frames:
            _, buf = cv2.imencode(".jpg", f)
            b64_list.append(base64.b64encode(buf).decode())

        return b64_list, "image/jpeg", "image"

    except Exception:
        raise


