from pathlib import Path
from typing import Dict, Optional
import yaml
import re
from logging_config import get_logger

logger = get_logger(__name__)


class SkillLoader:
    """Claude Code 兼容的技能加载器"""

    def __init__(self, skill_dir: str = None):
        if skill_dir is None:
            skill_dir = str(Path(__file__).parent / "skills")
        self.skill_dir = Path(skill_dir)
        self.skills: Dict[str, dict] = {}
        self._scan_skills()

    def _scan_skills(self):
        """扫描所有 SKILL.md 文件"""
        for skill_md_path in self.skill_dir.glob("**/SKILL.md"):
            content = skill_md_path.read_text(encoding='utf-8')
            parsed = self._parse_skill_md(content)

            if parsed:
                name = parsed.get("name") or skill_md_path.parent.name
                self.skills[name] = {
                    "name": name,
                    "brief": parsed.get("description", ""),
                    "detail": parsed["detail"],
                    "source_path": str(skill_md_path)
                }

    def _parse_skill_md(self, content: str) -> Optional[dict]:
        """解析 SKILL.md 的 frontmatter"""
        match = re.match(r'^---\n(.*?)\n---\n(.*)$', content, re.DOTALL)
        if not match:
            return None

        frontmatter = yaml.safe_load(match.group(1))
        body = match.group(2).strip()

        return {
            "name": frontmatter.get("name"),
            "description": frontmatter.get("description", ""),
            "detail": body
        }

    def get_brief_prompt(self) -> str:
        """返回简短描述列表，注入 system prompt"""
        if not self.skills:
            return "无可用技能"

        lines = ["可用技能："]
        for name, info in self.skills.items():
            lines.append(f"- {name}: {info['brief']}")
        return "\n".join(lines)

    def get_skill_detail(self, skill_name: str) -> Optional[str]:
        """返回技能的纯文本内容（已去除 markdown 格式）"""
        skill = self.skills.get(skill_name)
        if not skill:
            return None
        return self._strip_markdown(skill["detail"])

    def _strip_markdown(self, text: str) -> str:
        """去除 markdown 格式，保留纯文本"""
        # 去除标题标记 (# ## ###)
        text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
        # 去除粗体/斜体 (**text** → text, *text* → text)
        text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
        text = re.sub(r'\*(.+?)\*', r'\1', text)
        # 去除行内代码标记 (`code` → code)
        text = re.sub(r'`([^`]+)`', r'\1', text)
        # 去除无序列表标记（行首的 - * +）
        text = re.sub(r'^[\s]*[-*+]\s+', '', text, flags=re.MULTILINE)
        # 去除有序列表标记（行首的 1. 2. 等）
        text = re.sub(r'^[\s]*\d+\.\s+', '', text, flags=re.MULTILINE)
        # 去除水平分割线
        text = re.sub(r'^[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)
        return text

    def list_skills(self) -> list:
        """列出所有技能名"""
        return list(self.skills.keys())


# 模块级加载器实例
loader = SkillLoader()