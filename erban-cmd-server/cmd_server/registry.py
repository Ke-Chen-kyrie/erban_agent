"""命令注册表: 扫描 runtime/*/commands, introspection 读 argparse, 生成 shell 包装.

单一事实源 = 每个命令的 argparse (build_parser). 参数从 parser._actions 读出,
无 sidecar manifest. list 每次自扫自带刷新, 重建注册表 + 重生成包装.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
import importlib.util
import os
from pathlib import Path
import sys
import traceback
from types import ModuleType

from .config import Config


@dataclass
class Command:
    """一条注册命令 (commands/<name>.py, 参数来自 build_parser)."""

    name: str
    py_path: Path  # commands/<name>.py
    feature_dir: Path  # 所属客户端库目录
    description: str = ""
    params: list = field(default_factory=list)  # [{name, alias, required, type, choices, default, help}]
    error: str = ""  # 加载/派参失败 traceback (含文件与行号), 空=正常


# argparse type 可调用 → 人类可读类型名 (None/无显式 type 视为字符串)
_TYPE_NAMES = {int: "数字", float: "数字", str: "字符串"}


def _human_type(action) -> str:
    """type 映射: int/float→数字, 其余→字符串."""
    a_type = getattr(action, "type", None)
    if a_type in _TYPE_NAMES:
        return _TYPE_NAMES[a_type]
    if a_type is None:
        return "数字" if isinstance(getattr(action, "default", None), (int, float)) else "字符串"
    return "字符串"


def _is_action(action) -> bool:
    """过滤非命令参数 (parser 根 action 与自动 --help)."""
    option_strings = getattr(action, "option_strings", None) or []
    if set(option_strings) == {"-h", "--help"}:
        return False
    if not option_strings and getattr(action, "dest", None) is None:
        return False  # parser 根 action
    return True


def _arg_param(action) -> dict:
    """argparse action → 人类可读参数字典 (给 /list 与 /catalog)."""
    option_strings = list(getattr(action, "option_strings", None) or [])
    if option_strings:
        long_name = next((o.lstrip("-") for o in option_strings if o.startswith("--")), None)
        alias = next((o.lstrip("-") for o in option_strings if not o.startswith("--")), None)
    else:
        long_name = action.dest
        alias = None
    positional = not option_strings
    required = bool(getattr(action, "required", False))
    if positional:  # 位置参数: nargs 缺省视作必填
        required = action.nargs not in (None, "?")
    return {
        "name": long_name,
        "alias": alias,
        "positional": positional,
        "required": required,
        "type": _human_type(action),
        "choices": list(action.choices) if action.choices else None,
        "default": action.default if getattr(action, "default", None) is not None else None,
        "help": (action.help or "").strip() or None,
    }


def _inspect_command(py_path: Path) -> tuple[str, list, str]:
    """import 命令模块, 调 build_parser, introspection 取描述与参数.

    单条命令失败不影响整体扫描: 坏命令回退空描述/空参数, 但保留 traceback
    (含文件与行号) 供 /list 上报, 不再静默吞.
    """
    module, error = _load_module(py_path)
    if module is None:
        return "", [], error
    description = ""
    params: list = []
    build = getattr(module, "build_parser", None)
    parser = None
    if callable(build):
        try:
            parser = build()
        except Exception:
            error = traceback.format_exc()
    if parser is not None:
        description = (parser.description or "").strip()
        params = [_arg_param(a) for a in parser._actions if _is_action(a)]  # noqa: SLF001  # 刻意 introspection
    if not description and module.__doc__:
        description = module.__doc__.strip().splitlines()[0] or ""
    return description, params, error


def _load_module(py_path: Path) -> tuple[ModuleType | None, str]:
    """用唯一名加载命令模块 (重复 rescan 复用同 spec, exec_module 重执行).

    exec_module 抛 SyntaxError/ImportError 时格式化 traceback 返回, 让上层能报出文件+行号.
    """
    try:
        spec = importlib.util.spec_from_file_location(f"_cmd_{py_path.stem}", py_path)
        if spec is None or spec.loader is None:
            return None, f"无法定位模块: {py_path}"
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module, ""
    except Exception:
        return None, traceback.format_exc()


class Registry:
    """内存命令注册表. 白名单 = 扫描命令 加 系统命令 (见 STRUCTURE 决策7)."""

    def __init__(self, config: Config, bin_dir: Path):
        self.config = config
        self.bin_dir = bin_dir  # 包装脚本输出目录
        self.commands: dict[str, Command] = {}
        self.whitelist: set[str] = set()

    def _purge_client_modules(self) -> None:
        """清掉已 import 的 client 库缓存, 让命令重扫时重新读磁盘最新码.

        服务进程长期存活, client 库 (`ident_client`/`robot_client`/`web_client`)
        启动后只 import 一次, 其源文件热改后 `sys.modules` 里仍是旧模块 — 命令文件里
        `from ident_client import ...` 命中旧缓存会报 ImportError, introspection 空。
        每次 rescan 前清空这一层即可实时反映磁盘改动, 无需重启容器。
        """
        clients = ("ident_client", "robot_client", "web_client", "emote_client")
        for name in [k for k in list(sys.modules) if k.split(".")[0] in clients]:
            sys.modules.pop(name, None)

    def rescan(self) -> dict:
        """重扫注册表, 重建 + 重生成包装, 返回 {added, removed, changed} 清单."""
        self._purge_client_modules()  # 先清缓存, 保证命令 re-import 到最新 client 库
        old_names = set(self.commands)
        features = self._find_features()

        new_commands: dict[str, Command] = {}
        for py_file in self._iter_command_files(features):
            name = py_file.stem
            description, params, error = _inspect_command(py_file)
            new_commands[name] = Command(
                name=name,
                py_path=py_file,
                feature_dir=py_file.parent.parent,
                description=description,
                params=params,
                error=error,
            )

        new_names = set(new_commands)
        added = new_names - old_names
        removed = old_names - new_names
        changed = {name for name in new_names & old_names if self.commands[name].py_path != new_commands[name].py_path}

        self.commands = new_commands
        # 白名单 = 扫描命令 加 系统命令 (两源合并)
        self.whitelist = new_names | set(self.config.system_commands)
        self._generate_wrappers(features)

        return {
            "added": sorted(added),
            "removed": sorted(removed),
            "changed": sorted(changed),
            "command_count": len(self.commands),
            "errors": {name: c.error for name, c in sorted(new_commands.items()) if c.error},  # 坏命令 traceback (文件+行号)
        }

    def list_commands(self) -> list[dict]:
        """全部注册命令名+描述 (LLM 发现用).

        只回名+一行描述 (描述省 context 又能选命令). 参数 schema 不给 --
        正确调参的源是 /catalog 页 (现场生成), 不是 /list.
        """
        return [
            {"name": c.name, "description": c.description} for c in sorted(self.commands.values(), key=lambda c: c.name)
        ]

    def _find_features(self) -> list[Path]:
        """runtime/* 客户端库目录 (带 commands/ 子目录), 滤掉 exclude_scan_paths."""
        root = self.config.runtime
        excluded = {root / p for p in self.config.exclude_scan_paths}
        return sorted(p for p in root.iterdir() if p.is_dir() and p not in excluded and (p / "commands").is_dir())

    def _iter_command_files(self, features: list[Path]):
        for feat in features:
            yield from sorted((feat / "commands").glob("*.py"))

    def _generate_wrappers(self, features: list[Path]) -> int:
        """每个命令包成可用命令名: 写 bin/<name> 脚本, tmp+mv 原子写."""
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        count = 0
        for py_file in self._iter_command_files(features):
            target = self.bin_dir / py_file.stem
            content = f'#!/bin/sh\nexec python3 "{py_file}" "$@"\n'
            tmp = target.with_suffix(f".tmp.{os.getpid()}")  # pid 后缀: 多 worker 并发 rescan 不抢同名 tmp
            tmp.write_text(content, encoding="utf-8")
            tmp.chmod(0o755)
            tmp.rename(target)  # 原子替换, 并发 list 不写出半文件
            count += 1
        return count
