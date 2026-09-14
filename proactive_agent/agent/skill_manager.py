import re
from pathlib import Path
from typing import Dict, Optional

import httpx
import yaml
from dotenv import load_dotenv
from langfuse import Langfuse

from logging_config import get_logger

logger = get_logger(__name__)

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
_cert_path = Path(__file__).resolve().parent.parent / "certs" / "langfuse-erban-cert.pem"
_langfuse = Langfuse(httpx_client=httpx.Client(verify=str(_cert_path))) if _cert_path.exists() else Langfuse()


class SkillLoader:
    """从 Langfuse 拉取技能内容的加载器。"""

    def __init__(self):
        self.skills: Dict[str, dict] = {}
        self._load_from_langfuse()

    def _load_from_langfuse(self):
        raw = _langfuse.get_prompt(
            name="proactive_agent_realtime/langfuse_skill_names",
            label="production",
        ).prompt
        prompt_names = [line.strip() for line in raw.strip().split("\n") if line.strip()]

        for prompt_name in prompt_names:
            try:
                prompt = _langfuse.get_prompt(name=prompt_name, label="production")
                meta = self._parse_frontmatter(prompt.prompt)
                skill_name = meta.get("name", prompt_name.rsplit("/", 1)[-1].replace("_", "-"))
                self.skills[skill_name] = {
                    "name": skill_name,
                    "brief": meta.get("description", ""),
                    "detail": prompt.prompt,
                }
            except Exception as e:
                logger.error(f"从 Langfuse 加载技能 {prompt_name} 失败: {e}")

    @staticmethod
    def _parse_frontmatter(text: str) -> dict:
        m = re.match(r'^---\n(.*?)\n---', text, re.DOTALL)
        if not m:
            return {}
        return yaml.safe_load(m.group(1)) or {}

    def get_brief_prompt(self) -> str:
        if not self.skills:
            return "无可用技能"

        lines = ["可用技能："]
        for name, info in self.skills.items():
            lines.append(f"- {name}: {info['brief']}")
        return "\n".join(lines)

    def get_skill_detail(self, skill_name: str) -> Optional[str]:
        skill = self.skills.get(skill_name)
        if not skill:
            return None
        return skill["detail"]

    def list_skills(self) -> list:
        return list(self.skills.keys())


loader = SkillLoader()