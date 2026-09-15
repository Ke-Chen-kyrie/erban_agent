#!/usr/bin/env python3
"""机器人工具函数 —— 实现 + OpenAI function-calling schema 生成 + 运行时 dispatch。"""

import asyncio
import inspect
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Literal, get_args, get_origin, get_type_hints

import aiohttp
from dotenv import load_dotenv
from langfuse import get_client

from agent.utils import (
    _get_card_control,
    _looks_like_error,
    set_mic_muted,
)
from agent.interrupt_state import (
    consume_interrupt,
    interrupt_event,
    set_tool_audio_active,
)
from agent.voice_state import (
    _VALID_VOICES,
    _set_current_voice,
    get_current_voice,
)
from agent.skill_manager import loader as skill_loader
from config import (
    SHELL_PROXY_URL,
    SHELL_PROXY_TIMEOUT,
    LOCAL_CLI_DIR,
    LOCAL_CLI_COMMANDS,
    LOCAL_CLI_TIMEOUT,
)
from logging_config import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════
#  Langfuse 初始化
# ═══════════════════════════════════════════════════════════

load_dotenv(Path(__file__).resolve().parent.parent / ".env")
_langfuse = get_client()

_EXECUTE_SHELL_DESCRIPTION = _langfuse.get_prompt(
    name="proactive_agent_realtime/execute_shell_tool_description",
    label="production",
).prompt


# ═══════════════════════════════════════════════════════════
#  工具函数实现
# ═══════════════════════════════════════════════════════════

async def execute_shell(command: str) -> str:
      """
    在机器人本体上安全地执行 Shell/Bash 命令并返回结果。
    支持标准 Linux 命令和预定义的自定义 CLI 命令。自定义命令主要用于机器人
    控制、身份识别、信息查询等场景。
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

      # 本地 CLI：命令首词命中白名单 → 在本机 local_cli 环境本地执行，不走远程代理
      try:
          argv = shlex.split(command)
      except ValueError as e:
          return f"❌ 命令解析失败: {e}"
      if argv and argv[0] in LOCAL_CLI_COMMANDS:
          return await _run_local_cli(argv)

      url = SHELL_PROXY_URL

      payload = {"cmd": command}

      # 执行期间静音麦克风，屏蔽机器人自身动作/回放声触发 VAD 打断
      set_mic_muted(True)

   #  await asyncio.sleep(60)
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
      finally:
          set_mic_muted(False)


async def _run_local_cli(argv: list[str]) -> str:
    """在本机 local_cli 环境里执行本地 CLI 命令（如 sing），返回其输出。

    执行期间统一走「可打断」路径（vad / keyword 模式都生效）：不静音麦克风、关流，
    让本地 KWS 独占音频队列听打断词，命中即 kill 该命令。
    """
    tool = argv[0]
    exe = os.path.join(LOCAL_CLI_DIR, ".venv", "bin", tool)

    # 本地 CLI 执行期间统一走「可打断」路径（vad / keyword 均支持）：
    # 不静音麦克风、关流，让本地 KWS 独占音频队列听打断词，命中即 kill 该命令。
    set_tool_audio_active(True)

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, *argv[1:],
            cwd=LOCAL_CLI_DIR,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        evt = interrupt_event()
        comm = asyncio.create_task(proc.communicate())
        loop = asyncio.get_running_loop()
        deadline = loop.time() + LOCAL_CLI_TIMEOUT
        while not comm.done():
            if evt.is_set():                     # 命中打断词 → 打断
                consume_interrupt()              # 清 latch，防返回 _consume 后误触发
                try:
                    proc.kill()
                except Exception:
                    pass
                await comm
                logger.info(f"[tools] 本地命令 {tool} 被打断词中断，已 kill 子进程")
                return f"✅ 已停止：用户中途打断了命令「{tool}」。必须询问用户接下来想做什么，不要擅自决策或自行继续。"
            if loop.time() > deadline:           # 超时
                try:
                    proc.kill()
                except Exception:
                    pass
                await comm
                return f"❌ 本地命令 {tool} 超时（>{LOCAL_CLI_TIMEOUT} 秒），已中断。"
            await asyncio.sleep(0.1)
        stdout, stderr = comm.result()

        out = stdout.decode(errors="ignore").strip()
        err = stderr.decode(errors="ignore").strip()
        if proc.returncode != 0:
            return f"❌ 本地命令 {tool} 执行失败: {err or out or '未知错误'}"
        return out or f"✅ 已执行 {tool}"
    except FileNotFoundError:
        return f"❌ 未找到本地命令 {tool}（应为 {exe}）。请先在 local_cli 里 pip install 对应命令包。"
    except asyncio.TimeoutError:
        if proc is not None and proc.returncode is None:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
        return f"❌ 本地命令 {tool} 超时（>{LOCAL_CLI_TIMEOUT} 秒），已中断。"
    except Exception as e:
        return f"❌ 本地命令 {tool} 异常: {e}"
    finally:
        set_tool_audio_active(False)


def is_local_cli_shell(name: str, args: dict[str, Any]) -> bool:
    """判断某次工具调用是否会落到本地 CLI（如 sing），需要在本机独占扬声器。"""
    if name != "execute_shell":
        return False
    try:
        argv = shlex.split((args or {}).get("command", "") or "")
    except ValueError:
        return False
    return bool(argv) and argv[0] in LOCAL_CLI_COMMANDS


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
    - Tina: 甜甜Tina，温热奶茶般的亲切声音，默认音色，支持中文（普通话）、英语、法语、德语、俄语、意大利语、西班牙语、葡萄牙语、日语、韩语、泰语等

    """
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

def get_omni_voice() -> str:
    """获取当前语音输出音色名称"""
    return f"当前音色为 {get_current_voice()}"

async def get_skill_detail(skill_name: str) -> str:
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


# ═══════════════════════════════════════════════════════════
#  schema 自动生成
# ═══════════════════════════════════════════════════════════

def _doc_desc(func) -> str:
    """从函数 docstring 提取完整描述作为工具描述。"""
    doc = inspect.getdoc(func)
    if not doc:
        return ""
    return doc.strip()


def _type_to_json(py_type) -> dict:
    """Python 类型 → JSON Schema 类型。"""
    origin = get_origin(py_type)
    if origin is Literal:
        args = get_args(py_type)
        if all(isinstance(a, str) for a in args):
            return {"type": "string", "enum": list(args)}
    if py_type is str:
        return {"type": "string"}
    if py_type is int:
        return {"type": "integer"}
    if py_type is float:
        return {"type": "number"}
    if py_type is bool:
        return {"type": "boolean"}
    if origin is list:
        return {"type": "array", "items": {"type": "string"}}
    return {"type": "string"}


def _build_tool(func, *, description: str | None = None, params: dict[str, str] | None = None) -> dict[str, Any]:
    """从函数签名 + docstring 自动生成 OpenAI function-calling 工具定义。"""
    hints = get_type_hints(func)
    sig = inspect.signature(func)
    param_descs = params or {}
    properties = {}
    required = []

    for name, param in sig.parameters.items():
        if name in ("self", "runtime"):
            continue
        py_type = hints.get(name, str)
        prop = dict(_type_to_json(py_type))
        if name in param_descs:
            prop["description"] = param_descs[name]
        properties[name] = prop
        if param.default is inspect.Parameter.empty:
            required.append(name)

    return {
        "type": "function",
        "function": {
            "name": func.__name__,
            "description": description if description is not None else _doc_desc(func),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            } if properties else {"type": "object", "properties": {}},
        },
    }


# ── 工具定义 + 映射 ──

REALTIME_TOOLS: list[dict[str, Any]] = [
    _build_tool(execute_shell, description=_EXECUTE_SHELL_DESCRIPTION, params={"command": "需要执行的 Shell 命令字符串"}),
    _build_tool(get_volume),
    _build_tool(set_volume, params={"volume": "音量百分比，0-100 之间的整数"}),
    _build_tool(set_omni_voice, params={"voice": "音色名称"}),
    _build_tool(get_omni_voice),
    _build_tool(get_skill_detail, params={"skill_name": "技能名称，如 feed-food、feed-water、rehab-exercise"}),
    _build_tool(go_sleep),
]

_TOOL_MAP: dict[str, Any] = {
    "execute_shell": execute_shell,
    "get_volume": get_volume,
    "set_volume": set_volume,
    "set_omni_voice": set_omni_voice,
    "get_omni_voice": get_omni_voice,
    "get_skill_detail": get_skill_detail,
    "go_sleep": go_sleep,
}

def build_tools_for_realtime() -> list[dict[str, Any]]:
    """返回适用于 Qwen-Omni-Realtime API 的工具定义列表。"""
    return REALTIME_TOOLS


async def execute_tool(name: str, arguments: dict[str, Any]) -> str:
    """根据工具名和参数执行工具，返回结果字符串。"""
    func = _TOOL_MAP.get(name)
    if func is None:
        return f"客户端未找到工具: {name}"

    try:
        if asyncio.iscoroutinefunction(func):
            result = await func(**arguments)
        else:
            result = func(**arguments)
        return str(result)
    except Exception as e:
        logger.error(f"[tools] 工具 {name} 执行失败: {e}")
        return f"工具执行失败: {e}"