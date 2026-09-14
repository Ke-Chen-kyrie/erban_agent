"""统一提示词 —— Qwen 后端推理的所有系统提示词。"""

from pathlib import Path

import httpx
from dotenv import load_dotenv
from langfuse import Langfuse

load_dotenv(Path(__file__).resolve().parent / ".env")

_cert_path = Path(__file__).resolve().parent / "certs" / "langfuse-erban-cert.pem"
_langfuse = Langfuse(httpx_client=httpx.Client(verify=str(_cert_path))) if _cert_path.exists() else Langfuse()


def _get_base_prompt() -> str:
    """从 Langfuse 拉取 production 版本的系统提示词。"""
    return _langfuse.get_prompt(
        name="proactive_agent_realtime/system_prompt",
        label="production",
    ).prompt


# ═══════════════════════════════════════════════════════════
#  Qwen 后端推理提示词
# ═══════════════════════════════════════════════════════════

AGENT_SYSTEM_PROMPT = _get_base_prompt()


# ═══════════════════════════════════════════════════════════
#  系统提示词构建
# ═══════════════════════════════════════════════════════════

def build_system_prompt(all_users: list | None = None) -> str:
    """构建系统提示词。"""
    parts = [AGENT_SYSTEM_PROMPT]

    # 所有注册用户列表
    if all_users:
        user_lines = []
        for u in all_users:
            uid = u.get("user_id", "")
            name = u.get("name", "")
            if name:
                user_lines.append(f"  - {name}（ID: {uid}）")
        if user_lines:
            parts.append(
                "\n【已注册用户】\n以下是系统中所有已注册用户，画面中绿框标注的人员来自此列表：\n"
                + "\n".join(user_lines)
            )
    parts.append("\n请选择合适的技能："
    )
    from agent.skill_manager import loader as skill_loader
    parts.append(skill_loader.get_brief_prompt())

    return "\n".join(parts)
