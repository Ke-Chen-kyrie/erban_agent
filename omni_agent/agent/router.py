"""
多模态路由 —— 根据用户意图决定是否发送视频/图片给 LLM。

两级决策：
  1. 关键词快速匹配（零延迟）
  2. 小模型兜底（~100ms，需 ROUTER_VISUAL_FALLBACK_ENABLED=true）
"""

import time
from config import (
    SMALL_INTENT_LLM_CONFIG,
    ROUTER_ENABLED,
    ROUTER_VISUAL_ENABLED,
    ROUTER_VISUAL_FALLBACK_ENABLED,
)
from agent.prompt import VISUAL_PROMPT, DIRECTED_PROMPT
from logging_config import get_logger

logger = get_logger(__name__)

VISUAL_KEYWORDS = [
    # 看/观察
    "看看", "看一下", "看一眼", "你看", "看到", "看见", "能看见", "能看到", "瞧", "瞅", "望", "观察", "查看",
    # 画面/媒体
    "画面", "图片", "照片", "图像", "视频", "录像", "影像", "场景",
    # 环境/方位
    "环境", "周围", "附近", "眼前", "这里", "那里", "那边", "跟前",
    "前面", "后面", "旁边", "上面", "下面", "左边", "右边",
    "桌子上", "地上", "沙发上", "床上", "手里",
    # 是什么/谁
    "这是什么", "那是啥", "什么东西", "哪个", "是谁", "谁在", "几个人",
    "什么样子", "什么样", "长什么样",
    # 有没有/在哪
    "有没有", "在哪里", "在哪", "找", "找找", "帮我找", "找一下", "不见了", "找不到",
    # 状态确认
    "还在吗", "走了吗", "来了吗", "到了吗", "开着吗", "关了吗", "亮着吗", "还在不在",
    "开着没", "关着没", "亮了吗", "灭了吗",
    # 识人/辨认
    "有人吗", "谁来了", "认出", "认识", "这是谁", "辨认", "识别",
    # 穿着/动作
    "穿的", "拿着", "做什么", "在干嘛", "在干什么", "在做什么",
    # 数量
    "几个", "多少", "数一数", "数数",
    # 描述
    "描述", "形容", "扫描",
]

def _extract_recent_context(messages: list, rounds: int = 5) -> str:
    context_lines = []
    human_count = 0
    for msg in reversed(messages):
        if msg.type == "human":
            human_count += 1
            if human_count > rounds:
                break
            content = msg.content
            if isinstance(content, list):
                texts = [p.get("text", "") for p in content if isinstance(p, dict) and "text" in p]
                text = " ".join(texts) if texts else ""
            else:
                text = str(content) if content else ""
            if text.strip():
                context_lines.append(text.strip())
        elif msg.type == "ai":
            content = msg.content
            if isinstance(content, list):
                texts = [p.get("text", "") for p in content if isinstance(p, dict) and "text" in p]
                text = "".join(texts)
            else:
                text = str(content) if content else ""
            if text.strip():
                context_lines.append(f"小伴：{text.strip()}")
    context_lines.reverse()
    return "\n".join(context_lines)


def _dispatch_keyword(text: str) -> bool:
    for kw in VISUAL_KEYWORDS:
        if kw in text:
            logger.info(f"[router] 关键词命中: '{kw}'")
            return True
    return False


async def _dispatch_small_model(user_text: str, context: str) -> bool:
    from agent.agent import _get_llm_client

    client = _get_llm_client(SMALL_INTENT_LLM_CONFIG)
    prompt = VISUAL_PROMPT.format(context=context or "（无历史对话）", user_text=user_text)
    t0 = time.time()
    try:
        response = await client.chat.completions.create(
            model=SMALL_INTENT_LLM_CONFIG["model"],
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": user_text},
            ],
            extra_body={"enable_thinking": False},
            max_tokens=5,
            temperature=0.0,
        )
        elapsed = (time.time() - t0) * 1000
        result = response.choices[0].message.content.strip() if response.choices else "0"
        logger.info(f"[router] 小模型判断结果: '{result}' (耗时 {elapsed:.0f}ms)")
        return result == "1"
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.error(f"[router] 小模型调用失败: {e} (耗时 {elapsed:.0f}ms)，默认 false")
        return False


async def route(user_text: str, messages: list) -> bool:
    """判断是否需要发送视频/图片给 LLM，返回 send_visual。"""
    if not ROUTER_ENABLED or not ROUTER_VISUAL_ENABLED:
        return True
    if not user_text.strip():
        return False

    # 第一级：关键词
    if _dispatch_keyword(user_text):
        logger.info("[router] 视觉路由 → true（关键词）")
        return True

    # 第二级：小模型兜底（需显式启用）
    if ROUTER_VISUAL_FALLBACK_ENABLED:
        context = _extract_recent_context(messages, rounds=5)
        result = await _dispatch_small_model(user_text, context)
        logger.info(f"[router] 视觉路由 → {result}（小模型）")
        return result

    logger.info("[router] 视觉路由 → false（默认）")
    return False


# ─── 对话对象判断：用户是否在对小伴说话 ───

DIRECTED_KEYWORDS = [
    "小伴", "小伴小伴", "机器人", "管家",
]

# Chain 5 强负例：短碎片/语气词，直接判 0，省掉模型调用
DIRECTED_NEGATIVE_FRAGMENTS = {
    "咳咳", "嗯", "唉", "啧", "嘶", "哎呀", "哎哟", "哎", "啊",
}


def _dispatch_directed_keyword(text: str) -> bool:
    for kw in DIRECTED_KEYWORDS:
        if kw in text:
            logger.info(f"[directed] 关键词命中: '{kw}' → 对小伴说话")
            return True
    return False


def _dispatch_directed_negative(text: str) -> bool:
    """短碎片/语气词快速判负，返回 True 表示确认为非定向（不是对小伴说）。"""
    stripped = text.strip()
    if len(stripped) <= 3 and stripped in DIRECTED_NEGATIVE_FRAGMENTS:
        logger.info(f"[directed] 负例碎片命中: '{stripped}' → 不是对小伴说话")
        return True
    return False


async def _dispatch_directed_model(user_text: str, context: str, known_names: str = "") -> bool:
    from agent.agent import _get_llm_client

    client = _get_llm_client(SMALL_INTENT_LLM_CONFIG)
    prompt = DIRECTED_PROMPT.format(
        context=context or "（无历史对话）",
        user_text=user_text,
        known_names=known_names or "王爷爷/李奶奶/张爷爷/张护工/刘护士",
    )
    t0 = time.time()
    try:
        response = await client.chat.completions.create(
            model=SMALL_INTENT_LLM_CONFIG["model"],
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": "判断"},
            ],
            extra_body={"enable_thinking": False},
            max_tokens=5,
            temperature=0.0,
        )
        elapsed = (time.time() - t0) * 1000
        result = response.choices[0].message.content.strip() if response.choices else "0"
        logger.info(f"[directed] 模型判断结果: '{result}' (耗时 {elapsed:.0f}ms)")
        return result == "1"
    except Exception as e:
        elapsed = (time.time() - t0) * 1000
        logger.error(f"[directed] 模型调用失败: {e} (耗时 {elapsed:.0f}ms)，默认 true（不拦截）")
        return True


async def detect_directed_intent(user_text: str, messages: list = None, known_names: str = "") -> bool:
    """判断用户是否在对小伴说话，返回 True=对小伴说, False=不是对小伴说。"""
    if not user_text or not user_text.strip():
        return True

    # 关键词命中 → 直接放行
    if _dispatch_directed_keyword(user_text):
        return True

    # 短碎片/语气词 → 直接判负
    if _dispatch_directed_negative(user_text):
        return False

    # 模型判断
    context = _extract_recent_context(messages or [], rounds=5)
    result = await _dispatch_directed_model(user_text, context, known_names)
    logger.info(f"[directed] 对话对象判断 → {result}")
    return result