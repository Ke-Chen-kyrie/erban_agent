"""
Agent 客户端 —— 薄封装层，构建多模态消息并流式调用 agent。
"""

from typing import AsyncGenerator, Optional

from agent.agent import default_agent, stream_agent, get_messages
from agent.router import route
from langchain_core.messages import AIMessage, HumanMessage


class ChatSession:
    """多轮流式对话会话，直接调用 agent 进行流式推理。"""

    def __init__(self, thread_id: str = "admin"):
        self.thread_id = thread_id
        self.user_name = ""
        self.user_role = ""
        self.user_description = ""
        self.user_id = "unknown"
        self.possible_speakers: list = []

    def reset(self):
        self.user_id = "unknown"
        self.user_name = ""
        self.user_role = ""
        self.user_description = ""

    async def pre_route(self, user_text: str) -> bool:
        """预路由：在视频编码前判断是否需要发送视频/图片给 LLM。

        从 checkpointer 加载历史消息，调用 router 返回 send_visual。
        与声纹识别、视频编码并行执行。
        """
        messages = await get_messages(self.thread_id)
        return await route(user_text, messages)

    async def save_partial_response(self, text: str, user_input: str = ""):
        """保存被打断的部分 AI 回复到聊天历史。

        HumanMessage 已由 chat_stream 在 graph 运行前保存，这里只需保存部分 AI 文本。
        同时清空 user_text 防止仍在运行的 omni_node 完成后保存重复的 HumanMessage。
        """
        config = {"configurable": {"thread_id": self.thread_id}}
        if text.strip():
            # 检查最后一条 AI 消息是否已包含相同内容（omni_node 可能已完成）
            messages = await get_messages(self.thread_id)
            for m in reversed(messages):
                if isinstance(m, AIMessage):
                    existing = m.content
                    if isinstance(existing, list):
                        existing = "".join(p.get("text", "") for p in existing if isinstance(p, dict))
                    if existing and existing in text:
                        await default_agent.aupdate_state(config, {"user_text": ""}, as_node="omni")
                        return
                    break
            await default_agent.aupdate_state(
                config,
                {"messages": [AIMessage(content=text)], "user_text": ""},
                as_node="omni",
            )
        else:
            await default_agent.aupdate_state(config, {"user_text": ""}, as_node="omni")

    async def chat_stream(
        self,
        audio_base64: str = "",
        user_text: str = "",
        media: tuple = None,
        send_visual: Optional[bool] = None,
        gesture_mode: bool = False,
    ) -> AsyncGenerator[tuple[str, str], None]:
        """
        流式对话，yield (kind, payload) 元组。
        kind: "token" | "audio" | "custom" | "error"
        gesture_mode: True 时本轮为视频 VAD 静默手势交互，历史消息写 "[视频消息]"
        """
        config = {
            "configurable": {
                "thread_id": self.thread_id,
                "user_name": self.user_name,
                "user_role": self.user_role,
                "user_description": self.user_description,
                "user_id": self.user_id,
                "possible_speakers": self.possible_speakers,
            }
        }

        images_base64 = []
        videos_base64 = []
        if media:
            videos_base64, images_base64 = media
            videos_base64 = videos_base64 or []
            images_base64 = images_base64 or []

        # 在 graph 运行前保存 HumanMessage，避免 omni_node 内保存导致的重复问题
        # 通过 astream 的 messages 入参传入，不通过 aupdate_state，避免 LangGraph 的 as_node 歧义
        msgs_input = []
        if gesture_mode:
            display_text = f"{self.user_name}：{user_text}" if self.user_name else user_text
            human_content = [{"text": display_text}]
            for img in images_base64:
                mime = img.get("mime_type", "image/jpeg") if isinstance(img, dict) else "image/jpeg"
                b64 = img.get("image_base64", "") if isinstance(img, dict) else img
                human_content.append({"image": f"data:{mime};base64,{b64}"})
            for vid in videos_base64:
                mime = vid.get("mime_type", "video/avi") if isinstance(vid, dict) else "video/avi"
                b64 = vid.get("video_base64", "") if isinstance(vid, dict) else vid
                human_content.append({"video": f"data:{mime};base64,{b64}"})
            msgs_input = [HumanMessage(content=human_content)]
        elif user_text:
            display_text = f"{self.user_name}：{user_text}" if self.user_name else user_text
            human_content = [{"text": display_text}]
            for img in images_base64:
                mime = img.get("mime_type", "image/jpeg") if isinstance(img, dict) else "image/jpeg"
                b64 = img.get("image_base64", "") if isinstance(img, dict) else img
                human_content.append({"image": f"data:{mime};base64,{b64}"})
            for vid in videos_base64:
                mime = vid.get("mime_type", "video/avi") if isinstance(vid, dict) else "video/avi"
                b64 = vid.get("video_base64", "") if isinstance(vid, dict) else vid
                human_content.append({"video": f"data:{mime};base64,{b64}"})
            msgs_input = [HumanMessage(content=human_content)]

        extra_state = {
            "latest_audio_base64": audio_base64 or "",
            "user_text": user_text or "",
            "latest_images_base64": images_base64,
            "latest_videos_base64": videos_base64,
        }
        if send_visual is not None:
            extra_state["send_visual"] = send_visual
        if gesture_mode:
            extra_state["gesture_mode"] = True

        try:
            async for item in stream_agent(default_agent, msgs_input, config, extra_state=extra_state):
                if item["type"] == "token":
                    yield ("token", item["text"])
                elif item["type"] == "reasoning":
                    yield ("token", item["text"])
                elif item["type"] == "audio":
                    yield ("audio", item["data"])
                elif item["type"] == "audio_reset":
                    yield ("audio_reset", "")
                elif item["type"] == "message_end":
                    yield ("message_end", "")
                elif item["type"] == "custom":
                    data = item.get("data", {})
                    msg_text = data.get("message", "")
                    if msg_text:
                        yield ("custom", msg_text)
        except Exception as e:
            yield ("error", str(e))