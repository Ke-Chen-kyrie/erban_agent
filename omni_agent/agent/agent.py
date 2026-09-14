"""
Agent 核心 —— 状态定义、工具、图编译
"""
import asyncio
import base64
import cv2
import json
import os
import tempfile
from typing import Annotated, Any, Dict, NotRequired, TypedDict
from openai import AsyncOpenAI
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.utils.function_calling import convert_to_openai_function
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import StateGraph, START, END, add_messages
from langgraph.prebuilt import ToolNode
from langgraph.config import get_stream_writer

from agent.tools import (
    observe_environment,
    get_volume,
    set_volume,
    set_omni_voice,
    get_omni_voice,
    execute_shell,
    get_skill_detail,
    go_sleep,
    focus_on,
)
from agent.skill_manager import loader as skill_loader
from agent.utils import get_user_info, get_tts_fallback, set_router_start_time, set_router_end_time, set_agent_start_time, set_agent_prep_start_time
from agent.voice_state import get_current_voice
from agent.prompt import SYSTEM_PROMPT, SUMMARY_PROMPT
from agent.router import route
from config import (
    QWEN_LLM_CONFIG, BYTEDANCE_LLM_CONFIG, MIMO_LLM_CONFIG, SMALL_INTENT_LLM_CONFIG,
    OMNI_AUDIO_FORMAT, HISTORY_KEEP_ROUNDS, LLM_PROVIDER, AUDIO_SOURCES,
    AGENT_MAX_RETRIES, AGENT_RETRY_DELAYS,
)
from logging_config import get_logger

logger = get_logger(__name__)

# ---------- LLM 客户端 ----------

_llm_client_cache: dict[tuple, AsyncOpenAI] = {}

def _get_llm_client(config: dict) -> AsyncOpenAI:
    """根据配置字典返回 AsyncOpenAI 客户端（复用连接池）。"""
    key = (config["api_key"], config["base_url"])
    if key not in _llm_client_cache:
        _llm_client_cache[key] = AsyncOpenAI(
            api_key=config["api_key"],
            base_url=config["base_url"],
        )
    return _llm_client_cache[key]


# ---------- State ----------

class State(TypedDict):
    messages: Annotated[list, add_messages]

    user_text: str
    latest_audio_base64: str
    latest_images_base64: list          # 用户主动上传的图片（base64 字符串）
    latest_videos_base64: list          # 用户主动上传的视频
    pending_tool_calls: list

    camera_captured_images: list        # observe_environment 工具拍摄的图片（base64 字符串）
    camera_captured_videos: list        # observe_environment 工具录制的视频（base64 字符串）
    camera_captured_audios: list        # observe_environment 工具录制的音频（base64 字符串，16kHz int16 WAV）

    conversation_summary: str           # 历史对话压缩摘要
    compressed_up_to_index: int         # 已压缩到的消息索引位置

    send_visual: NotRequired[bool]      # 预路由结果：是否发送视频/图片给 LLM
    gesture_mode: NotRequired[bool]     # 视频 VAD 模式：本轮为静默手势交互


# ---------- 工具 ----------

DEFAULT_TOOLS = [
    execute_shell,
    observe_environment,
    get_volume,
    set_volume,
    get_skill_detail,
    go_sleep,
    focus_on,
]

if AUDIO_SOURCES != "tts":
    DEFAULT_TOOLS.extend([get_omni_voice, set_omni_voice])


OPENAI_TOOLS = [{"type": "function", "function": convert_to_openai_function(t)} for t in DEFAULT_TOOLS]
TOOLS_MAP = {t.name: t for t in DEFAULT_TOOLS}


# ---------- 视频帧数检测 ----------

MIN_VIDEO_FRAMES = 5  # Qwen-Omni 最低约 4-5 帧, 少于此数拆为图片


def _video_to_content_blocks(video_url: str):
    """将 video data URL 转为 content blocks。帧数不足时拆为 JPEG 图片。"""
    if not isinstance(video_url, str) or ";base64," not in video_url:
        return [{"type": "video_url", "video_url": {"url": video_url}}]

    try:
        _, b64 = video_url.split(";base64,", 1)
        data = base64.b64decode(b64)
    except Exception:
        return [{"type": "video_url", "video_url": {"url": video_url}}]

    with tempfile.NamedTemporaryFile(suffix=".avi", delete=False) as f:
        f.write(data)
        tmp_path = f.name

    try:
        cap = cv2.VideoCapture(tmp_path)
        if not cap.isOpened():
            cap.release()
            return [{"type": "video_url", "video_url": {"url": video_url}}]

        frame_count = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
        if frame_count >= MIN_VIDEO_FRAMES:
            cap.release()
            return [{"type": "video_url", "video_url": {"url": video_url}}]

        frames = []
        while True:
            ret, frame = cap.read()
            if not ret:
                break
            _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 85])
            frames.append(base64.b64encode(buf).decode())
        cap.release()

        if not frames:
            return [{"type": "video_url", "video_url": {"url": video_url}}]

        logger.info(f"[video] {len(data)}B 视频 ({frame_count}帧<{MIN_VIDEO_FRAMES}) → {len(frames)} 张图片")
        return [
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b}"}}
            for b in frames
        ]
    except Exception as e:
        logger.warning(f"[video] 视频帧检测失败: {e}")
        return [{"type": "video_url", "video_url": {"url": video_url}}]
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _langchain_msg_to_openai(msg) -> dict:
    """将 LangChain 消息转为 OpenAI 兼容多模态格式。"""
    if msg.type == "human":
        content = msg.content
        if isinstance(content, str):
            return {"role": "user", "content": [{"type": "text", "text": content}]}
        elif isinstance(content, list):
            openai_content = []
            for part in content:
                if isinstance(part, dict):
                    if "text" in part:
                        openai_content.append({"type": "text", "text": part["text"]})
                    elif "image" in part:
                        openai_content.append({
                            "type": "image_url",
                            "image_url": {"url": part["image"]},
                        })
                    elif "video" in part:
                        openai_content.extend(_video_to_content_blocks(part["video"]))
                    elif "audio" in part:
                        if LLM_PROVIDER == "doubao":
                            audio_part = {"data": part["audio"], "format": "wav"}
                        else:
                            audio_part = {"data": f"data:audio/wav;base64,{part['audio']}", "format": "wav"}
                        openai_content.append({
                            "type": "input_audio",
                            "input_audio": audio_part,
                        })
                elif isinstance(part, str):
                    openai_content.append({"type": "text", "text": part})
            return {"role": "user", "content": openai_content}
    elif msg.type == "ai":
        content = msg.content
        if isinstance(content, list):
            text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and "text" in p]
            content = "".join(text_parts)
        result: dict = {"role": "assistant", "content": content or ""}
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            openai_tool_calls = []
            for tc in msg.tool_calls:
                name = tc.get("name", "")
                args = tc.get("args", {})
                openai_tc = {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False) if isinstance(args, dict) else str(args),
                    },
                }
                openai_tool_calls.append(openai_tc)
            result["tool_calls"] = openai_tool_calls
        return result
    elif msg.type == "tool":
        return {
            "role": "tool",
            "content": str(msg.content),
            "tool_call_id": msg.tool_call_id,
        }
    elif msg.type == "system":
        return {"role": "system", "content": msg.content}
    return {"role": "user", "content": str(msg.content)}


# ---------- 系统提示词构建 ----------

def _build_system_prompt(base_prompt: str, user_name: str, user_role: str, user_description: str, possible_speakers: list, user_id: str = "", conversation_summary: str = "") -> str:
    """构建包含说话人上下文、历史摘要和可用技能的系统提示词。"""
    parts = [base_prompt]

    # 注入历史对话摘要
    if conversation_summary:
        parts.append(f"【历史对话摘要】\n以下是你与用户此前对话的压缩摘要，请参考以保持上下文连贯：\n{conversation_summary}")

    # 注入可用技能列表
    parts.append(skill_loader.get_brief_prompt())

    if possible_speakers:
        speaker_infos = []
        for u in possible_speakers:
            uid = u.get("user_id", "")
            name = u.get("name", "")
            role = u.get("role", "")
            if name and uid:
                info = f"{name}(ID:{uid}"
                if role:
                    info += f", {role}"
                info += ")"
                speaker_infos.append(info)
            elif name:
                speaker_infos.append(name)
        if speaker_infos:
            parts.append(f"库中已注册的可能说话人: {', '.join(speaker_infos)}。")

    return "\n".join(parts)


def _build_identity_marker(label: str, user_name: str, user_id: str, user_role: str, user_description: str) -> str:
    """构建身份识别标记，注入到用户消息中。label 为 '声纹识别' 或 '人脸识别'。"""
    if not user_name:
        return ""
    if user_id == "system":
        return ""
    if user_id == "unknown":
        return f"[{label}: 未匹配, 路人]"
    parts = [f"[{label}: {user_name}"]
    if user_id:
        parts.append(f", ID: {user_id}")
    if user_role:
        parts.append(f", 角色: {user_role}")
    if user_description:
        parts.append(f", 描述: {user_description}")
    parts.append("]")
    return "".join(parts)


async def _compress_conversation(existing_summary: str, messages: list, start_index: int, compress_before: int) -> str:
    """使用千问小模型将旧消息 + 旧摘要压缩为新的滚动摘要。

    只提取 [start_index, compress_before) 区间的新增消息，已摘要过的消息由 existing_summary 覆盖。"""
    conversation_lines = []
    for i, msg in enumerate(messages):
        if i < start_index:
            continue
        if i >= compress_before:
            break
        if msg.type == "human":
            content = msg.content
            if isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and "text" in p]
                text = " ".join(text_parts) if text_parts else "[多模态输入]"
            else:
                text = str(content)
            if text:
                conversation_lines.append(f"用户：{text}")
        elif msg.type == "ai":
            content = msg.content
            if isinstance(content, list):
                text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and "text" in p]
                text = "".join(text_parts)
            else:
                text = str(content) if content else ""
            # 跳过纯工具调用（无文本内容）
            if text and text.strip():
                conversation_lines.append(f"助手：{text}")

    if not conversation_lines and not existing_summary:
        return ""

    new_messages_text = "\n".join(conversation_lines)

    user_content_parts = []
    if existing_summary:
        user_content_parts.append(f"【已有摘要】\n{existing_summary}")
    user_content_parts.append(f"【新的对话内容】\n{new_messages_text}")

    client = _get_llm_client(SMALL_INTENT_LLM_CONFIG)
    try:
        response = await client.chat.completions.create(
            model=SMALL_INTENT_LLM_CONFIG["model"],
            messages=[
                {"role": "system", "content": SUMMARY_PROMPT},
                {"role": "user", "content": "\n\n".join(user_content_parts)},
            ],
            extra_body={"enable_thinking": False},
        )
        result = response.choices[0].message.content.strip() if response.choices else ""
        return result if result else None
    except Exception as e:
        logger.error(f"[记忆压缩] 摘要生成失败: {e}，保留旧摘要")
        return None


# ---------- Agent 图 ----------

def create_agent(tools=None, system_prompt=None):
    """构建并编译 agent 图。"""
    if tools is None:
        tools = DEFAULT_TOOLS
    if system_prompt is None:
        system_prompt = SYSTEM_PROMPT

    agent_builder = StateGraph(State)
    tool_node = ToolNode(tools=tools)

    async def omni_node(state: State, config):
        """Omni 节点：调用 OpenAI 兼容 API，流式输出文本+音频。"""
        writer = get_stream_writer()
        messages = state.get("messages", [])
        # 从全局共享状态读取（由 main.py 后台任务写入），无需等待
        user_name, user_role, user_description = get_user_info(timeout=0.1)
        possible_speakers = config.get("configurable", {}).get("possible_speakers", [])
        user_id = config.get("configurable", {}).get("user_id", "")
        voice = get_current_voice()

        # 从 state 获取历史对话摘要，注入系统提示词
        conversation_summary = state.get("conversation_summary", "") or ""
        prompt = _build_system_prompt(system_prompt, user_name, user_role, user_description, possible_speakers, user_id, conversation_summary)

        # 1. 历史消息压缩：从 state 读取已压缩位置，避免摘要与历史消息重复
        keep_from = state.get("compressed_up_to_index", 0) or 0

        # 找到最新一轮 tool-call 的 human 消息索引（用于保留其多媒体内容）
        tool_call_human_indices = set()
        for i, msg in enumerate(messages):
            if msg.type == "human":
                if i + 1 < len(messages):
                    next_msg = messages[i + 1]
                    if next_msg.type == "ai" and hasattr(next_msg, "tool_calls") and next_msg.tool_calls:
                        for j in range(i + 2, len(messages)):
                            if messages[j].type == "tool":
                                tool_call_human_indices.add(i)
                                break
                            elif messages[j].type == "human":
                                break
        latest_tool_call_human = max(tool_call_human_indices) if tool_call_human_indices else -1

        final_messages = [{"role": "system", "content": prompt}]

        # 当前轮用户输入（提前读取，用于跳过历史消息中已保存的 HumanMessage）
        user_text = state.get("user_text", "")
        gesture_mode = state.get("gesture_mode", False)
        audio_base64 = state.get("latest_audio_base64", "")
        images_base64 = state.get("latest_images_base64", []) or []
        videos_base64 = state.get("latest_videos_base64", []) or []

        # 找到最后一条 HumanMessage，如果它包含当前 user_text 则跳过
        # (chat_stream 已提前保存到 state，后面会作为 current_content 重新加入)
        last_human_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].type == "human":
                last_human_idx = i
                break

        # 一旦有新的人类消息进来（非 tool-call human 自身），不再保留多媒体
        # observe_environment 注入的视频/音频在 final_messages 而非 state，不影响此判断
        if latest_tool_call_human >= 0 and last_human_idx != latest_tool_call_human:
            latest_tool_call_human = -1

        for i, msg in enumerate(messages):
            if i < keep_from:
                continue
            # 跳过与当前 user_text 匹配的最后一条 HumanMessage（chat_stream 已保存，避免重复）
            if i == last_human_idx and msg.type == "human":
                content = msg.content
                if isinstance(content, list):
                    text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and "text" in p]
                    text = "".join(text_parts)
                else:
                    text = str(content)
                # 跳过 chat_stream 已保存的重复 HumanMessage：语音轮次匹配 user_text，手势轮次匹配 [系统事件]
                if user_text and user_text in text:
                    continue
                if gesture_mode and "[系统事件]" in text:
                    continue
            if msg.type == "human":
                content = msg.content
                # 最新 tool-call 轮的 human 消息保留多媒体，让 LLM 看到原始视觉上下文
                if i == latest_tool_call_human and isinstance(content, list):
                    has_multimedia = any(
                        isinstance(p, dict) and any(k in p for k in ("image", "video", "audio"))
                        for p in content
                    )
                    if has_multimedia:
                        final_messages.append(_langchain_msg_to_openai(msg))
                        continue
                if isinstance(content, list):
                    text_parts = [p.get("text", "") for p in content if isinstance(p, dict) and "text" in p]
                    text = " ".join(text_parts) if text_parts else "[历史消息]"
                else:
                    text = str(content)
                final_messages.append({"role": "user", "content": [{"type": "text", "text": text}]})
            else:
                final_messages.append(_langchain_msg_to_openai(msg))

        # 2. 当前轮用户主动输入（音频/视频/图片）—— 经路由器决策

        # 路由决策：判断是否需要发送视频/图片给 LLM
        import time as _time
        # 唤醒应答固定文本，跳过意图识别
        if user_text == "你好，小伴！":
            send_visual = bool(images_base64 or videos_base64)
            logger.info(f"[omni] 唤醒应答，跳过路由: send_visual={send_visual}")
        elif state.get("send_visual") is not None:
            send_visual = state["send_visual"]
            logger.info(f"[omni] 使用预路由结果: send_visual={send_visual}")
        else:
            set_router_start_time(_time.time())
            send_visual = await route(user_text, messages)
            set_router_end_time(_time.time())
            logger.info(f"[omni] 路由决策: send_visual={send_visual}")

        current_content = []
        # 声纹/人脸识别标记，只在有用户实际输入时注入（工具链续接轮次不注入）
        has_user_input = bool(audio_base64 or user_text or images_base64 or videos_base64)
        if has_user_input:
            if gesture_mode:
                marker = _build_identity_marker("人脸识别", user_name, user_id, user_role, user_description)
            else:
                marker = _build_identity_marker("声纹识别", user_name, user_id, user_role, user_description)
            if marker:
                current_content.append({"type": "text", "text": marker})
        # 视频按路由决策发送
        if send_visual:
            for img_b64 in images_base64:
                mime = img_b64.get("mime_type", "image/jpeg") if isinstance(img_b64, dict) else "image/jpeg"
                b64 = img_b64.get("image_base64", "") if isinstance(img_b64, dict) else img_b64
                current_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
            for vid_b64 in videos_base64:
                mime = vid_b64.get("mime_type", "video/avi") if isinstance(vid_b64, dict) else "video/avi"
                b64 = vid_b64.get("video_base64", "") if isinstance(vid_b64, dict) else vid_b64
                current_content.extend(_video_to_content_blocks(f"data:{mime};base64,{b64}"))
        # 音频始终发送
        if audio_base64:
            if LLM_PROVIDER == "doubao":
                audio_part = {"data": audio_base64, "format": "wav"}
            else:
                audio_part = {"data": f"data:audio/wav;base64,{audio_base64}", "format": "wav"}
            current_content.append({
                "type": "input_audio",
                "input_audio": audio_part,
            })
            # 有音频时跳过用户语音文字，但保留 gesture 文本和 [系统事件] 文本
            if user_text:
                if gesture_mode or "[系统事件]" in user_text:
                    keep_text = user_text
                    if keep_text:
                        current_content.append({"type": "text", "text": keep_text})
        elif user_text:
            current_content.append({"type": "text", "text": user_text})

        if current_content:
            final_messages.append({"role": "user", "content": current_content})

        # 3. observe_environment 工具结果额外注入（独立字段，杜绝残留）
        camera_images = state.get("camera_captured_images", []) or []
        camera_videos = state.get("camera_captured_videos", []) or []
        camera_audios = state.get("camera_captured_audios", []) or []
        # 扫描末尾所有连续 ToolMessage，任一个是 observe_environment 就注入
        has_camera_tool = False
        # 诊断：打印末尾 5 条消息的类型和 name
        tail_msgs = []
        for msg in messages[-5:]:
            name = getattr(msg, "name", "")
            tail_msgs.append(f"{msg.type}({name})" if name else msg.type)
        logger.debug(f"[omni] 末尾5条消息: {' → '.join(tail_msgs)}")
        for msg in reversed(messages):
            if isinstance(msg, ToolMessage):
                if msg.name == "observe_environment":
                    has_camera_tool = True
                    break
            else:
                break
        logger.debug(f"[omni] camera_images={len(camera_images)}张, camera_videos={len(camera_videos)}个, camera_audios={len(camera_audios)}个, has_camera_tool={has_camera_tool}")
        if has_camera_tool:
            for b64 in camera_images:
                final_messages.append({
                    "role": "user",
                    "content": [{
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                    }]
                })
            # 视频 + 音频成对注入同一条消息
            for i, b64 in enumerate(camera_videos):
                content_blocks = _video_to_content_blocks(f"data:video/avi;base64,{b64}")
                if i < len(camera_audios):
                    audio_b64 = camera_audios[i]
                    if LLM_PROVIDER == "doubao":
                        audio_part = {"data": audio_b64, "format": "wav"}
                    else:
                        audio_part = {"data": f"data:audio/wav;base64,{audio_b64}", "format": "wav"}
                    content_blocks.append({
                        "type": "input_audio",
                        "input_audio": audio_part,
                    })
                final_messages.append({"role": "user", "content": content_blocks})
            # 多余的音频单独发
            for j in range(len(camera_videos), len(camera_audios)):
                audio_b64 = camera_audios[j]
                if LLM_PROVIDER == "doubao":
                    audio_part = {"data": audio_b64, "format": "wav"}
                else:
                    audio_part = {"data": f"data:audio/wav;base64,{audio_b64}", "format": "wav"}
                final_messages.append({
                    "role": "user",
                    "content": [{"type": "input_audio", "input_audio": audio_part}]
                })
            logger.debug(f"[omni] 已注入 {len(camera_images)} 图 + {len(camera_videos)} 视频(含{min(len(camera_videos), len(camera_audios))}条音频) 到 final_messages")

        # --- 打印发给 Omni 的完整消息 ---
        omni_lines = ["\n===== Omni 消息 ====="]
        for idx, m in enumerate(final_messages):
            role = m.get("role", "?")
            content = m.get("content", "")
            tool_calls = m.get("tool_calls")
            tc_id = m.get("tool_call_id", "")

            # 构建角色标签
            if tool_calls:
                tc_parts = []
                for tc in tool_calls:
                    name = tc.get("function", {}).get("name", tc.get("name", "?"))
                    args_str = tc.get("function", {}).get("arguments", "")
                    if args_str:
                        tc_parts.append(f"{name}({args_str})")
                    else:
                        tc_parts.append(name)
                label = f"{role}(→{', '.join(tc_parts)})"
            elif tc_id:
                label = f"{role}(→tool:{tc_id})"
            else:
                label = f"{role}"

            # 格式化 content
            if isinstance(content, str):
                body = content if content else "(empty)"
            elif isinstance(content, list):
                parts = []
                for p in content:
                    if isinstance(p, dict):
                        ptype = p.get("type", "?")
                        if ptype == "text":
                            parts.append(f"text({p.get('text', '')})")
                        elif ptype == "image_url":
                            url = p.get("image_url", {}).get("url", "")
                            size = len(url)
                            parts.append(f"image({size} bytes)")
                        elif ptype == "video_url":
                            url = p.get("video_url", {}).get("url", "")
                            size = len(url)
                            parts.append(f"video({size} bytes)")
                        elif ptype == "input_audio":
                            data = p.get("input_audio", {}).get("data", "")
                            size = len(data)
                            parts.append(f"audio({size} bytes)")
                        else:
                            parts.append(ptype)
                    else:
                        parts.append(type(p).__name__)
                body = "[" + ", ".join(parts) + "]"
            else:
                body = str(content) if content else "(empty)"

            omni_lines.append(f"  [{idx}] {label}")
            omni_lines.append(f"         {body}")
        omni_lines.append("=========================\n")
        logger.info("\n".join(omni_lines))

        # 4. 模型选择：始终使用大模型
        _use_tts = AUDIO_SOURCES == "tts" and not get_tts_fallback()
        if LLM_PROVIDER == "doubao":
            selected_model = BYTEDANCE_LLM_CONFIG["model"]
        elif LLM_PROVIDER == "xiaomi":
            selected_model = MIMO_LLM_CONFIG["model"]
        else:
            selected_model = QWEN_LLM_CONFIG["model"]

        # 5. 调用 API（带重试，应对 SSE 流中途断开）
        _audio_mode_label = "TTS" if _use_tts else f"Omni(voice={voice})"
        logger.info(f"Omni 正在生成回复（model={selected_model}, audio={_audio_mode_label}）...")
        set_agent_prep_start_time(_time.time())
        ai_full_text = ""
        tool_calls_buffer: Dict[int, Dict[str, Any]] = {}
        max_retries = AGENT_MAX_RETRIES
        retry_delays = [float(x.strip()) for x in AGENT_RETRY_DELAYS.split(",")]
        _audio_bytes_from_api = 0
        _audio_chunks_from_api = 0

        # 选择客户端和 API 参数（TTS/Omni 统一用 Chat Completions，格式相同）
        if LLM_PROVIDER == "doubao":
            if not BYTEDANCE_LLM_CONFIG["api_key"]:
                raise RuntimeError("ARK_API_KEY 未配置，请在 .env 中设置 ARK_API_KEY=ark-xxx")
            client = _get_llm_client(BYTEDANCE_LLM_CONFIG)
            api_kwargs = {
                "model": selected_model,
                "messages": final_messages,
                "tools": OPENAI_TOOLS,
                "stream": True,
                "stream_options": {"include_usage": True},
                "extra_body": {"enable_thinking": False},
            }
        elif LLM_PROVIDER == "xiaomi":
            if not MIMO_LLM_CONFIG["api_key"]:
                raise RuntimeError("MIMO_API_KEY 未配置，请在 .env 中设置 MIMO_API_KEY=sk-xxx")
            client = _get_llm_client(MIMO_LLM_CONFIG)
            api_kwargs = {
                "model": selected_model,
                "messages": final_messages,
                "tools": OPENAI_TOOLS,
                "stream": True,
                "stream_options": {"include_usage": True},
                "extra_body": {"thinking": {"type": "disabled"}},
            }
        else:
            client = _get_llm_client(QWEN_LLM_CONFIG)
            api_kwargs = {
                "model": selected_model,
                "messages": final_messages,
                "tools": OPENAI_TOOLS,
                "stream": True,
                "stream_options": {"include_usage": True},
                "extra_body": {"enable_thinking": False},
            }
            if not _use_tts:
                api_kwargs["modalities"] = ["text", "audio"]
                api_kwargs["audio"] = {"voice": voice, "format": OMNI_AUDIO_FORMAT}

        for attempt in range(max_retries + 1):
            if attempt > 0:
                logger.warning(f"流断开，第 {attempt} 次重试...")
                if not _use_tts:
                    writer({"type": "audio_reset"})
                ai_full_text = ""
                tool_calls_buffer.clear()
                _audio_bytes_from_api = 0
                _audio_chunks_from_api = 0

            try:
                set_agent_start_time(_time.time())
                completion = await client.chat.completions.create(**api_kwargs)

                async for chunk in completion:
                    if not chunk.choices:
                        continue

                    delta = chunk.choices[0].delta

                    if delta.content:
                        ai_full_text += delta.content
                        writer({"type": "token", "text": delta.content})

                    if not _use_tts and hasattr(delta, "audio") and delta.audio:
                        audio_b64 = delta.audio.get("data", "")
                        if audio_b64:
                            _audio_bytes_from_api += len(audio_b64)
                            _audio_chunks_from_api += 1
                            writer({"type": "audio", "data": audio_b64})

                    if delta.tool_calls:
                        for tc in delta.tool_calls:
                            idx = tc.index
                            if idx not in tool_calls_buffer:
                                tool_calls_buffer[idx] = {
                                    "id": tc.id or "",
                                    "name": tc.function.name or "",
                                    "arguments": tc.function.arguments or "",
                                }
                            else:
                                if tc.id:
                                    tool_calls_buffer[idx]["id"] = tc.id
                                if tc.function.name:
                                    tool_calls_buffer[idx]["name"] = tc.function.name
                                if tc.function.arguments:
                                    tool_calls_buffer[idx]["arguments"] += tc.function.arguments

                break  # 正常结束

            except Exception as e:
                if attempt < max_retries:
                    logger.warning(f"流异常: {e}，{retry_delays[attempt]}s 后重试...")
                    await asyncio.sleep(retry_delays[attempt])
                else:
                    logger.error(f"流重试耗尽 ({max_retries}次): {e}")
                    raise

        # 5. 构建返回结果
        if tool_calls_buffer:
            final_tool_calls: list = []
            for tc in tool_calls_buffer.values():
                try:
                    final_tool_calls.append({
                        "name": tc["name"],
                        "args": json.loads(tc["arguments"]) if tc["arguments"] else {},
                        "id": tc["id"],
                        "type": "tool_call",
                    })
                except Exception:
                    final_tool_calls.append({
                        "name": tc["name"],
                        "args": {},
                        "id": tc["id"],
                        "type": "tool_call",
                    })
            logger.info(f"捕获到工具调用请求: {final_tool_calls}")
            if ai_full_text:
                logger.info(f"工具请求附带文字: {ai_full_text}")
            msgs_out = [AIMessage(content=ai_full_text, tool_calls=final_tool_calls)]
            if has_camera_tool:
                msgs_out.insert(0, HumanMessage(content="[系统事件]"))
            return {
                "messages": msgs_out,
                "latest_audio_base64": "",
                "latest_images_base64": [],
                "latest_videos_base64": [],
                "user_text": "",
                "pending_tool_calls": final_tool_calls,
                "camera_captured_images": [],
                "camera_captured_videos": [],
                "camera_captured_audios": [],
                "send_visual": None,
                "gesture_mode": False,
            }
        else:
            if LLM_PROVIDER == "doubao":
                logger.info(
                    f"豆包回复完毕（TTS），文本长度: {len(ai_full_text)} 字符"
                )
            elif LLM_PROVIDER == "xiaomi":
                logger.info(
                    f"小米回复完毕（TTS），文本长度: {len(ai_full_text)} 字符"
                )
            else:
                _audio_info = "TTS" if _use_tts else "原生音频"
                logger.info(
                    f"千问回复完毕（{_audio_info}），文本长度: {len(ai_full_text)} 字符, "
                    f"音频: {_audio_chunks_from_api} chunks / {_audio_bytes_from_api} bytes (b64) "
                    f"≈ {_audio_bytes_from_api * 3 / 4 / 2 / 24000:.2f}s"
                )
                # 诊断：文本长度与音频时长不匹配时告警（中文约 5-7 字/秒）
                audio_dur = _audio_bytes_from_api * 3 / 4 / 2 / 24000
                expected_dur = len(ai_full_text) / 6.0  # 假设平均语速 6 字/秒
                if audio_dur > 0 and expected_dur > 0 and audio_dur < expected_dur * 0.5:
                    logger.warning(
                        f"[音频截断检测] 文本 {len(ai_full_text)} 字 → 预期音频 ~{expected_dur:.1f}s, "
                        f"实际音频 ~{audio_dur:.1f}s (差异 >50%)，可能是 API 流式音频生成不完整"
                    )
            msgs_out = [AIMessage(content=ai_full_text)]
            if has_camera_tool:
                msgs_out.insert(0, HumanMessage(content="[系统事件]"))
            return {
                "messages": msgs_out,
                "latest_audio_base64": "",
                "latest_images_base64": [],
                "latest_videos_base64": [],
                "user_text": "",
                "pending_tool_calls": [],
                "camera_captured_images": [],
                "camera_captured_videos": [],
                "camera_captured_audios": [],
                "send_visual": None,
                "gesture_mode": False,
            }

    agent_builder.add_node("omni", omni_node)
    agent_builder.add_node("tools", tool_node)

    async def memory_compress_node(state: State):
        """记忆压缩节点：未压缩 human 消息超过 HISTORY_KEEP_ROUNDS 时，将更早的消息压缩进滚动摘要，保留最近 HISTORY_KEEP_ROUNDS 轮在上下文中。"""
        messages = state.get("messages", [])
        existing_summary = state.get("conversation_summary", "") or ""
        compressed_up_to = state.get("compressed_up_to_index", 0) or 0

        # 工具链未完成时跳过压缩（末尾是 ToolMessage 说明还有工具在执行）
        if not messages or messages[-1].type == "tool":
            return {}

        # 只统计未压缩部分的 human 消息
        uncompressed_human_indices = [
            i for i, msg in enumerate(messages) if msg.type == "human" and i >= compressed_up_to
        ]

        # 未压缩部分不足 15 轮，跳过
        if len(uncompressed_human_indices) <= HISTORY_KEEP_ROUNDS:
            return {}

        # 保留最近 HISTORY_KEEP_ROUNDS 轮，压缩更早的消息
        all_human_indices = [i for i, msg in enumerate(messages) if msg.type == "human"]
        keep_rounds = min(HISTORY_KEEP_ROUNDS, len(all_human_indices))
        new_compressed_up_to = all_human_indices[-keep_rounds]

        logger.info(
            f"[记忆压缩] 超过 {HISTORY_KEEP_ROUNDS} 轮对话，压缩消息 [{compressed_up_to} → {new_compressed_up_to})，"
            f"旧摘要长度: {len(existing_summary)} 字符"
        )

        new_summary = await _compress_conversation(
            existing_summary, messages, compressed_up_to, new_compressed_up_to
        )

        if new_summary:
            logger.info(
                f"[记忆压缩] 摘要生成完成，长度: {len(new_summary)} 字符（前80字）: {new_summary[:80]}..."
            )
            return {
                "conversation_summary": new_summary,
                "compressed_up_to_index": new_compressed_up_to,
            }
        else:
            logger.warning(
                f"[记忆压缩] 摘要生成返回空，保留旧摘要，但仍推进压缩位置到 {new_compressed_up_to} 避免重复重试"
            )
            return {"compressed_up_to_index": new_compressed_up_to}

    agent_builder.add_node("memory_compress", memory_compress_node)

    async def inject_camera_frame(state: State, config):
        """observe_environment 执行后，将新图片/视频/音频追加到对应字段（而非 latest_* 字段）。"""
        messages = state.get("messages", [])
        new_images = []
        new_videos = []
        new_audios = []
        logger.debug(f"[inject_camera] 被调用, messages 共 {len(messages)} 条")
        for msg in reversed(messages):
            if not isinstance(msg, ToolMessage):
                logger.debug(f"[inject_camera] 遇到非 ToolMessage ({type(msg).__name__}), 停止扫描")
                break
            logger.debug(f"[inject_camera] 检查 ToolMessage: name={msg.name}, content 前120字={str(msg.content)[:120]}")
            if msg.name == "observe_environment":
                content = msg.content
                if isinstance(content, str):
                    # 解析视频路径
                    if "视频路径:" in content:
                        vpath = content.split("视频路径: ")[-1].split(",")[0].strip()
                        if os.path.exists(vpath):
                            try:
                                with open(vpath, "rb") as f:
                                    b64 = base64.b64encode(f.read()).decode("utf-8")
                                new_videos.append(b64)
                                logger.debug(f"[inject_camera] 成功读取视频, base64 长度={len(b64)}")
                            except OSError as e:
                                logger.error(f"[inject_camera] 读取视频失败: {e}")
                    # 解析音频路径
                    if "音频路径:" in content:
                        apath = content.split("音频路径: ")[-1].strip()
                        if os.path.exists(apath):
                            try:
                                with open(apath, "rb") as f:
                                    b64 = base64.b64encode(f.read()).decode("utf-8")
                                new_audios.append(b64)
                                logger.debug(f"[inject_camera] 成功读取音频, base64 长度={len(b64)}")
                            except OSError as e:
                                logger.error(f"[inject_camera] 读取音频失败: {e}")
                    # 解析图片路径（单帧模式，排除视频/音频路径）
                    if "路径:" in content and "视频路径:" not in content and "音频路径:" not in content:
                        path = content.split("路径: ")[-1].strip()
                        logger.debug(f"[inject_camera] 找到 observe_environment 图片, 路径={path}, 存在={os.path.exists(path)}")
                        if os.path.exists(path):
                            try:
                                with open(path, "rb") as f:
                                    b64 = base64.b64encode(f.read()).decode("utf-8")
                                new_images.append(b64)
                                logger.debug(f"[inject_camera] 成功读取图片, base64 长度={len(b64)}")
                            except OSError as e:
                                logger.error(f"[inject_camera] 读取图片失败: {e}")
                        else:
                            logger.warning(f"[inject_camera] 文件不存在: {path}")
        result = {}
        if new_images:
            existing = state.get("camera_captured_images", []) or []
            result["camera_captured_images"] = existing + new_images
        if new_videos:
            existing_v = state.get("camera_captured_videos", []) or []
            result["camera_captured_videos"] = existing_v + new_videos
        if new_audios:
            existing_a = state.get("camera_captured_audios", []) or []
            result["camera_captured_audios"] = existing_a + new_audios
        if result:
            logger.debug(f"[inject_camera] 返回 {len(new_images)} 图 + {len(new_videos)} 视频 + {len(new_audios)} 音频")
        else:
            logger.debug(f"[inject_camera] 未找到新文件, 返回空")
        return result

    agent_builder.add_node("inject_camera", inject_camera_frame)

    def route_after_omni(state: State):
        if state.get("pending_tool_calls"):
            return "tools"
        return END

    agent_builder.add_conditional_edges(
        "omni", route_after_omni, {"tools": "tools", END: END}
    )
    agent_builder.add_edge("tools", "inject_camera")
    agent_builder.add_edge("inject_camera", "omni")
    agent_builder.add_edge(START, "memory_compress")
    agent_builder.add_edge("memory_compress", "omni")

    return agent_builder.compile(checkpointer=MemorySaver())


# 默认 agent 实例
default_agent = create_agent()


async def get_messages(thread_id: str) -> list:
    """从 checkpointer 加载指定 thread 的历史消息，返回 list。"""
    config = {"configurable": {"thread_id": thread_id}}
    checkpoint = await default_agent.checkpointer.aget_tuple(config)
    if checkpoint:
        return checkpoint[1].get("channel_values", {}).get("messages", [])
    return []


# ---------- 流式输出解析 ----------

async def stream_agent(agent, messages, config, extra_state: dict = None):
    """
    对 agent 发起 astream 调用，逐个 token/audio yield。
    yields: dict —— {"type": "token"|"reasoning"|"audio"|"custom", ...}
    """
    # 将 messages 和 extra_state 合并为 astream 的输入，一次性写入 state
    state_input = {"messages": messages}
    if extra_state:
        state_input.update(extra_state)

    async for chunk in agent.astream(
        state_input,
        stream_mode=["messages", "custom"],
        config=config,
    ):
        stream_type, data = chunk

        if stream_type == "custom":
            if isinstance(data, dict):
                data_type = data.get("type", "")
                if data_type == "token":
                    yield {"type": "token", "text": data.get("text", "")}
                elif data_type == "audio":
                    yield {"type": "audio", "data": data.get("data", "")}
                elif data_type == "audio_reset":
                    yield {"type": "audio_reset"}
                else:
                    yield {"type": "custom", "data": data}

        elif stream_type == "messages":
            msg_chunk, metadata = data

            if hasattr(msg_chunk, "additional_kwargs") and metadata.get("langgraph_node") not in ("tools", "inject_camera"):
                reasoning = msg_chunk.additional_kwargs.get("reasoning_content")
                if reasoning:
                    yield {"type": "reasoning", "text": reasoning}

            if isinstance(msg_chunk, AIMessage) and msg_chunk.content and metadata.get("langgraph_node") not in ("tools", "inject_camera"):
                content = msg_chunk.content
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and "text" in item:
                            yield {"type": "final_text", "text": item["text"]}
                elif isinstance(content, str):
                    yield {"type": "final_text", "text": content}

            # 助手消息结束时发信号，让 TTS 在工具执行前完成当前句子的播放
            if isinstance(msg_chunk, AIMessage) and hasattr(msg_chunk, "tool_calls") and msg_chunk.tool_calls \
                    and metadata.get("langgraph_node") not in ("tools", "inject_camera"):
                yield {"type": "message_end"}