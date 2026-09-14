#!/usr/bin/env python3
"""机器人工具函数 - 供Agent调用的工具"""

import asyncio
import json
import os
import re
import subprocess
import tempfile
import threading
from typing import Literal

import aiohttp
from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import ToolRuntime
from agent.utils import (
    _get_card_control,
    _looks_like_error,
)
from agent.voice_state import (
    _VALID_VOICES,
    _set_current_voice,
    get_current_voice,
)
from agent.skill_manager import loader as skill_loader
from config import (
    SHELL_PROXY_URL, AUDIO_SOURCES, BYTEDANCE_TTS_SPEAKER,
    OBSERVE_MAX_DURATION, OBSERVE_VIDEO_FPS, OBSERVE_SAMPLE_RATE,
    SHELL_PROXY_TIMEOUT, DEBUG_SAVE_MEDIA, CAMERA_TOPIC_HEAD,
    FOXGLOVE_BRIDGE_URL, CAMERA_SOURCE, LOCAL_CAMERA_INDEX,
    LOCAL_CAMERA_WIDTH, LOCAL_CAMERA_HEIGHT,
    JOINT_SERVICE_URL, JOINT_SERVICE_TIMEOUT,
)
from logging_config import get_logger

logger = get_logger(__name__)

from video.face_detect import _draw_face_detect




@tool
async def execute_shell(command: str) -> str:
      """
    在机器人本体上安全地执行 Shell/Bash 命令并返回结果。
    支持标准 Linux 命令和预定义的自定义 CLI 命令。自定义命令主要用于机器人
    控制、身份识别、信息查询等场景。

    Args:
        command (str): 需要执行的 Shell 命令字符串。支持以下类型：
            - 标准 Linux 命令（如 ls、cat、grep 等）
            - 自定义 CLI 命令：
                - 转身控制：
                    - `turn -t <target>`：转身面向指定目标，target 可选值：
                        - `user`：面向用户
                        - `table`：面向餐桌
                - 身份核查：
                    - `ident -u <用户ID>`：检查指定用户是否在摄像头画面中
                - 信息查询：
                    - `search <关键词>`：联网搜索，返回 5 条结果摘要
                    - `weather <城市>`：查询指定城市的实时天气和 7 天预报
                    - `cur-time`：获取当前日期和时间
    Returns:
        str: 命令执行结果摘要，包含以下信息：
            - 标准输出 (stdout)
            - 标准错误 (stderr)
            - 退出状态码 (exit code)

    Note:
        - 喂水、喂饭、康复运动等物理操作相关命令未在此列出，需要时请调用 `get_skill_detail` 函数查看。
        - 命令执行受安全策略约束，部分高危操作可能被限制。
        - 涉及特定用户的操作（如 ident、喂饭、喂水、康复运动）需要 -u <用户ID> 参数，
          你应根据声纹识别结果或用户指令自行决定服务对象。
      """

      # 拒绝危险 shell 模式
      dangerous_patterns = [
          r"\`[^`]*\`",       # 反引号命令替换
          r"\$\([^)]*\)",     # $() 命令替换
          r">\s*\S",          # 输出重定向
          r"<\s*\S",          # 输入重定向
          r"sudo\b",          # sudo
          r"rm\s+-rf",        # 强制删除
          r"/dev/",           # 设备文件
          r"mkfs\.",          # 格式化
          r"dd\s+if=",        # dd 磁盘操作
          r"chmod\s+777",     # 权限开放
      ]
      for pattern in dangerous_patterns:
          if re.search(pattern, command):
              return f"❌ 拒绝执行：命令包含不安全的模式 ({pattern})"

      url = SHELL_PROXY_URL

      payload = {"cmd": command}

      try:
          async with aiohttp.ClientSession() as session:
              async with session.post(url, json=payload, timeout=aiohttp.ClientTimeout(total=SHELL_PROXY_TIMEOUT)) as resp:
                  if resp.status == 200:
                      data = await resp.json()
                      output = data.get("output", "")
                      success = data.get("success", False)

                      if success:
                          # 远端服务可能返回 HTTP 200 但 output 中包含 Traceback，需拦截
                          if _looks_like_error(output):
                              return f"❌  执行失败（远端异常）:\n{output}"
                          return f"✅  执行成功:\n{output}"
                      else:
                          return f"❌  执行失败:\n{output}"
                  else:
                      error_text = await resp.text()
                      return f"❌  请求失败 (Status: {resp.status}):\n{error_text}"

      except asyncio.TimeoutError:
          return f"❌  请求超时 ({SHELL_PROXY_TIMEOUT}秒)"
      except Exception as e:
          return f"❌  发生未知异常: {str(e)}"

@tool
async def observe_environment(duration: int = 0) -> str:
    """
    观察环境。可以捕获单张图像，或录制一段带语音的短视频。

    duration: 录制时长（秒），0 表示只拍一张照片，>0 表示录制带语音的短视频（建议 3~10 秒）
    """
    topic = CAMERA_TOPIC_HEAD

    import cv2

    if duration <= 0:
        if CAMERA_SOURCE == "local":
            from video.foxglove_client import LocalCameraCapture
            cap = LocalCameraCapture(LOCAL_CAMERA_INDEX)
        else:
            from video.foxglove_client import FoxgloveImageCapture
            cap = FoxgloveImageCapture(topic)
        success, img = cap.read(timeout=3.0)
        cap.release()
        if not success or img is None:
            return f"无法从head摄像头获取画面，请检查摄像头连接"

        # 人脸检测并标注（用原始无压缩图像）
        img_bgr = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
        try:
            annotated = _draw_face_detect(img_bgr)
            if annotated is not img_bgr:
                img_bgr = annotated
        except Exception as e:
            logger.warning(f"[observe_environment] 人脸检测失败: {e}")

        persisted = os.path.join(tempfile.gettempdir(), f"latest_camera_head.jpg")
        cv2.imwrite(persisted, img_bgr)

        if DEBUG_SAVE_MEDIA:
            import shutil
            debug_dir = "debug_files"
            os.makedirs(debug_dir, exist_ok=True)
            shutil.copy(persisted, os.path.join(debug_dir, f"latest_camera_head.jpg"))

        file_size = os.path.getsize(persisted)
        return f"head摄像头最新一帧画面已捕获（{file_size} bytes），路径: {persisted}"
    else:
        # 录制视频 + 语音
        import numpy as np
        import wave
        import time as _time
        from video.foxglove_client import FoxgloveClient, decode_ros_image

        duration = min(duration, OBSERVE_MAX_DURATION)
        sample_rate = OBSERVE_SAMPLE_RATE
        video_fps = OBSERVE_VIDEO_FPS

        # 同时启动视频帧收集和音频录制
        frames = []
        audio_chunks = []
        video_done = threading.Event()
        audio_done = threading.Event()

        if CAMERA_SOURCE == "local":
            def _record_video():
                """在同步线程中通过本地摄像头收集视频帧"""
                import cv2
                cap = cv2.VideoCapture(LOCAL_CAMERA_INDEX)
                if not cap.isOpened():
                    logger.warning(f"[observe_environment] 无法打开本地摄像头 /dev/video{LOCAL_CAMERA_INDEX}")
                    video_done.set()
                    return
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, LOCAL_CAMERA_WIDTH)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, LOCAL_CAMERA_HEIGHT)
                start = _time.time()
                while _time.time() - start < duration:
                    ret, frame = cap.read()
                    if ret and frame is not None:
                        frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB))
                    _time.sleep(1.0 / video_fps)
                cap.release()
                video_done.set()
        else:
            def _record_video():
                """在同步线程中通过 Foxglove 收集视频帧"""
                from common.async_utils import run_async_in_thread

                async def _run():
                    client = FoxgloveClient(url=FOXGLOVE_BRIDGE_URL)
                    try:
                        await client.connect()
                        if topic not in client.topics:
                            logger.warning(f"[observe_environment] 话题不存在: {topic}")
                            video_done.set()
                            return
                        await client.subscribe(topic)
                        start = _time.time()
                        while _time.time() - start < duration:
                            result = await client.recv_message(timeout=0.5)
                            if result is not None:
                                t, msg, _ = result
                                if t == topic:
                                    img = decode_ros_image(msg)
                                    if img is not None:
                                        frames.append(img)
                            await asyncio.sleep(1.0 / video_fps)
                    except Exception as e:
                        logger.warning(f"[observe_environment] 视频录制异常: {e}")
                    finally:
                        await client.close()
                        video_done.set()

                run_async_in_thread(_run())

        def _record_audio():
            """从主音频采集队列录制麦克风音频"""
            try:
                from agent.utils import get_audio_queue
                q = get_audio_queue()
                if q is None:
                    logger.warning("[observe_environment] 音频队列未设置，跳过录音")
                    audio_done.set()
                    return

                # 清空队列中的残余数据
                while not q.empty():
                    try:
                        q.get_nowait()
                        q.task_done()
                    except Exception:
                        break

                # 收集 duration 秒的音频
                chunks = []
                samples_needed = int(duration * sample_rate)
                samples_collected = 0
                deadline = _time.time() + duration + 3.0

                while samples_collected < samples_needed and _time.time() < deadline:
                    try:
                        chunk = q.get(timeout=0.3)
                        chunks.append(chunk)
                        samples_collected += len(chunk)
                        q.task_done()
                    except Exception:
                        continue

                if chunks:
                    audio = np.concatenate(chunks)
                    if len(audio) > samples_needed:
                        audio = audio[:samples_needed]
                    audio_chunks.append(audio)
            except Exception as e:
                logger.warning(f"[observe_environment] 音频录制异常: {e}")
            finally:
                audio_done.set()

        video_thread = threading.Thread(target=_record_video, daemon=True)
        video_thread.start()
        audio_thread = threading.Thread(target=_record_audio, daemon=True)
        audio_thread.start()
        video_thread.join(timeout=duration + 10)
        audio_thread.join(timeout=duration + 5)

        if not frames:
            return f"无法从head摄像头录制视频，请检查摄像头连接"

        # 人脸检测：最后一帧标注人脸
        try:
            last_bgr = frames[-1][:, :, ::-1]  # RGB → BGR
            annotated = _draw_face_detect(last_bgr)
            if annotated is not last_bgr:
                frames[-1] = annotated[:, :, ::-1]  # BGR → RGB
        except Exception as e:
            logger.warning(f"[observe_environment] 视频人脸检测失败: {e}")

        # 保存无声视频 + 音频（分别保存，分别发给大模型）
        timestamp = int(_time.time() * 1000)
        video_path = os.path.join(tempfile.gettempdir(), f"observe_video_{timestamp}.avi")

        # 写无声 AVI
        h, w = frames[0].shape[:2]
        fourcc = cv2.VideoWriter_fourcc(*"MJPG")
        writer = cv2.VideoWriter(video_path, fourcc, video_fps, (w, h))
        if not writer.isOpened():
            fallback_path = os.path.join(tempfile.gettempdir(), f"observe_head_{timestamp}.jpg")
            cv2.imwrite(fallback_path, cv2.cvtColor(frames[0], cv2.COLOR_RGB2BGR))
            return f"视频编码器不可用，已保存head摄像头单帧画面，路径: {fallback_path}"

        for frame in frames:
            writer.write(frame[:, :, ::-1])  # RGB → BGR for OpenCV
        writer.release()

        # 保存音频
        has_audio = bool(audio_chunks) and len(audio_chunks[0]) > 0
        audio_path = ""
        if has_audio:
            audio_path = os.path.join(tempfile.gettempdir(), f"observe_audio_{timestamp}.wav")
            try:
                with wave.open(audio_path, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)  # int16
                    wf.setframerate(sample_rate)
                    audio_int16 = (audio_chunks[0] * 32767).astype(np.int16)
                    wf.writeframes(audio_int16.tobytes())
            except Exception as e:
                logger.warning(f"[observe_environment] 音频保存失败: {e}")
                audio_path = ""

        video_size = os.path.getsize(video_path)
        if DEBUG_SAVE_MEDIA:
            import shutil
            debug_dir = "debug_files"
            os.makedirs(debug_dir, exist_ok=True)
            shutil.copy(video_path, os.path.join(debug_dir, f"observe_head_{timestamp}.avi"))
            if has_audio and audio_path:
                shutil.copy(audio_path, os.path.join(debug_dir, f"observe_audio_{timestamp}.wav"))
        if has_audio and audio_path:
            audio_size = os.path.getsize(audio_path)
            return (f"已录制head摄像头{duration}秒视频（{video_size} bytes, {len(frames)}帧）和音频（{audio_size} bytes），"
                    f"视频路径: {video_path}, 音频路径: {audio_path}")
        else:
            return (f"已录制head摄像头{duration}秒无声视频（{video_size} bytes, {len(frames)}帧），"
                    f"视频路径: {video_path}")

@tool
def get_volume() -> str:
    """获取机器人当前说话的音量，返回 0-100 的百分比值"""
    try:
        card, control = _get_card_control()
        result = subprocess.run(
            ["amixer", "-c", str(card), "get", control],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return f"获取音量失败: {result.stderr.strip()}"
        match = re.search(r"(\d+)%", result.stdout)
        if match:
            vol = int(match.group(1))
            return f"当前音量为 {vol}%"
        return "无法解析音量信息"
    except FileNotFoundError:
        return "amixer 命令不可用，请确认 alsa-utils 已安装"
    except Exception as e:
        return f"获取音量异常: {e}"

@tool
def set_volume(volume: int) -> str:
    """
    设置机器人说话的音量
    volume: 音量百分比，0-100 之间的整数
    """
    if not 0 <= volume <= 100:
        return f"音量值 {volume} 无效，请使用 0-100 之间的整数"

    try:
        card, control = _get_card_control()
        result = subprocess.run(
            ["amixer", "-c", str(card), "set", control, f"{volume}%"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return f"设置音量失败: {result.stderr.strip()}"
        return f"音量已设置为 {volume}%"
    except FileNotFoundError:
        return "amixer 命令不可用，请确认 alsa-utils 已安装"
    except Exception as e:
        return f"设置音量异常: {e}"

@tool
def set_omni_voice(voice: Literal["Serena", "Theo Calm", "Harvey", "Mia", "Kiki", "Sunny", "Tina", "Ethan", "Andre", "Maia", "Liora Mira"]) -> str:
    """
    切换语音输出音色。
    推荐选择语速适中、沉稳清晰的声音。

    适老推荐音色列表：
    【日常陪伴类】
    - Serena: 温柔小姐姐，耐心清晰，最适合日常照料（默认推荐）
    - Mia: 舒然，慢生活博主，语速舒缓，从容不迫
    - Maia: 四月，知性与温柔碰撞，亲切自然
    - Liora Mira: 清欢，用声音织就烟火人间的温柔

    【情绪安抚/睡前陪伴类】
    - Theo Calm: 予安，沉稳疗愈，在言语间疗愈人心，适合安抚情绪与睡前陪伴
    - Harvey: 厚，低沉温和，带着咖啡与旧书的气息，浑厚有穿透力，适合听力稍弱的老人

    【活力阳光类（适合康复鼓励）】
    - Ethan: 晨煦，阳光温暖有朝气，适合晨间问候、康复锻炼鼓励
    - Andre: 安德雷，声音磁性沉稳，自然舒服，适合日常健康提醒

    【方言支持类（优先使用）】
    - Kiki: 粤语阿清，甜美港妹闺蜜，适合习惯听粤语的老人（检测到用户说粤语时必须先切换该音色再回答）
    - Sunny: 四川晴儿，甜到你心里的川妹子，适合习惯听四川话的老人

    【通用音色】
    - Tina: 甜甜Tina，温热奶茶般的亲切声音，默认音色

    使用建议：
    - 日常健康提醒/用药提醒 → Serena 或 Mia
    - 情绪低落/失眠时 → Theo Calm
    - 听力较弱长辈 → Harvey
    - 康复锻炼/晨间问候 → Ethan 或 Andre
    - 粤语/四川话长辈 → Kiki 或 Sunny
    """
    if AUDIO_SOURCES == "tts":
        return f"当前使用外部 TTS 服务（音色: {BYTEDANCE_TTS_SPEAKER}），无法动态切换音色"

    if voice not in _VALID_VOICES:
        names = {
            "Serena": "温柔耐心(日常首选)",
            "Theo Calm": "沉稳疗愈(安抚情绪)",
            "Harvey": "低沉浑厚(听力较弱)",
            "Mia": "舒缓从容(慢生活)",
            "Maia": "知性温柔(亲切自然)",
            "Liora Mira": "烟火温柔(温暖陪伴)",
            "Ethan": "阳光朝气(康复鼓励)",
            "Andre": "磁性沉稳(健康提醒)",
            "Kiki": "甜美粤语(粤语长辈)",
            "Sunny": "甜心四川话(四川长辈)",
            "Tina": "甜甜亲切"
        }
        hints = ", ".join(f"{k}({names.get(k, '')})" for k in sorted(_VALID_VOICES))
        return f"无效音色 '{voice}'，适老推荐可选: {hints}"

    _set_current_voice(voice)
    return f"音色已切换为 {voice}"

@tool
def get_omni_voice() -> str:
    """获取当前语音输出音色名称"""
    if AUDIO_SOURCES == "tts":
        return f"当前使用外部 TTS 服务（音色: {BYTEDANCE_TTS_SPEAKER}）"
    return f"当前音色为 {get_current_voice()}"

@tool
async def get_skill_detail(skill_name: str, runtime: ToolRuntime) -> str:
    """
    查询某个技能的详细操作指南

    Args:
        skill_name: 技能名称，如 "feed-food"（喂饭）、"feed-water"（喂水）、"rehab-exercise"（康复运动）
    """
    detail = skill_loader.get_skill_detail(skill_name)
    if not detail:
        available = skill_loader.list_skills()
        return f"未找到技能 '{skill_name}'，可用技能: {', '.join(available)}"
    return detail


@tool
async def go_sleep() -> str:
    """
    进入睡眠/待机模式，停止对话，不再监听和回应，直到用户重新说出唤醒词。

    调用时机：用户明确要求"闭嘴"、"别说话了"、"去睡觉"、"休息吧"、"下去吧"等。
    调用后系统将停止当前对话，需用户重新说唤醒词"小伴小伴"才能再次唤醒。

    重要：调用此工具前必须先输出一句简短的告别语（如"好的，有需要再叫我"），
    确保用户听到告别后再调用此工具进入睡眠。
    """
    from agent.utils import request_sleep_mode
    request_sleep_mode()
    return "已进入睡眠模式，等待下次唤醒。"


@tool
async def focus_on(body_parts: list[str]) -> str:
    """
    聚焦关注用户的特定身体部位，增强对该部位的感知与分析能力。
    当你需要仔细观察用户的某个身体部位状态时使用（如检查手部姿势、头部姿态、关节活动等）。

    Args:
        body_parts: 要聚焦的身体部位列表，支持中文或英文名称。
                   示例：["右手"]、["头部", "右肘"]、["整体"]
                   可用部位（24 个 SKEL 关节，中英文均可）：
                   骨盆(pelvis) 右髋(femur_r) 右膝(tibia_r) 右踝(talus_r) 右跟骨(calcn_r) 右趾(toes_r)
                   右肩胛(scapula_r) 右肩(humerus_r) 右肘(ulna_r) 右腕(radius_r) 右手(hand_r)
                   左髋(femur_l) 左膝(tibia_l) 左踝(talus_l) 左跟骨(calcn_l) 左趾(toes_l)
                   左肩胛(scapula_l) 左肩(humerus_l) 左肘(ulna_l) 左腕(radius_l) 左手(hand_l)
                   腰椎(lumbar_body) 胸椎(thorax) 头(head)
                   传入 ["整体"] 可聚焦全部 24 个关节
    """
    if not body_parts:
        return "❌ 请指定至少一个身体部位"

    payload = {part: 1 for part in body_parts}
    url = f"{JOINT_SERVICE_URL}/joint"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload,
                timeout=aiohttp.ClientTimeout(total=JOINT_SERVICE_TIMEOUT),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    parts_str = "、".join(body_parts)
                    return f"✅ 已聚焦身体部位: {parts_str}。返回数据: {json.dumps(data, ensure_ascii=False)}"
                else:
                    error_text = await resp.text()
                    return f"❌ 关节服务请求失败 (Status: {resp.status}): {error_text}"
    except asyncio.TimeoutError:
        return f"❌ 关节服务请求超时 ({JOINT_SERVICE_TIMEOUT}秒)"
    except Exception as e:
        return f"❌ 关节服务连接异常: {str(e)}"


