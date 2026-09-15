"""服务器自身配置加载.

config 只启动读一次 (生命周期不变), 改动需重启容器. 只留 server 关切:
来源 IP 白名单, 系统命令白名单, scan 排除. robot/web/ident 端点与密钥
是各 client 库 (robot_client/web_client/ident_client) 的配置, 由库自读, 不在此解析.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import os
from pathlib import Path

import yaml


def config_dir() -> Path:
    """容器自身配置挂载根. 本地测试用 CMD_CONFIG 覆盖, 默认 /config."""
    return Path(os.environ.get("CMD_CONFIG", "/config"))


def runtime_dir() -> Path:
    """runtime 挂载根 (客户端库 + 命令). 本地测试用 CMD_RUNTIME 覆盖, 默认 /runtime."""
    return Path(os.environ.get("CMD_RUNTIME", "/runtime"))


def load_config(root: Path | None = None) -> Config:
    """读 config.yaml; 缺文件或解析失败时返回全拒配置 (宁拒勿放).

    缺文件拒绝启动由 entrypoint 负责, 这里兜底全拒, 双保险.
    """
    root = root or config_dir()
    cfg_file = root / "config.yaml"
    if not cfg_file.exists():
        return Config(
            runtime=runtime_dir(),
            allow_ips=set(),
            system_commands=[],
            exclude_scan_paths=[],
            catalog_enable_test=False,
        )

    data = yaml.safe_load(cfg_file.read_text(encoding="utf-8")) or {}
    allow = {str(x) for x in data.get("allow_ips", []) or []}
    allow.add("127.0.0.1")  # localhost 恒兜底
    catalog = data.get("catalog", {}) or {}
    return Config(
        runtime=runtime_dir(),
        allow_ips=allow,
        system_commands=list(data.get("system_commands", []) or []),
        exclude_scan_paths=list(data.get("exclude_scan_paths", []) or []),
        catalog_enable_test=bool(catalog.get("enable_test", False)),
    )


@dataclasses.dataclass
class Config:
    """服务器自身运行时配置.

    allow_ips  全接口来源白名单, 缺省=全拒; 127.0.0.1 恒兜底加入
    system_commands  基础 shell 工具白名单 (非机器人命令)
    exclude_scan_paths  scan 排除的相对路径 (相对 runtime/)
    catalog_enable_test   /catalog 页是否启用"测试命令" UI (默认关, 宁拒勿放)

    robot/web/ident 端点见各 client 库 (client 配置 client 读).
    """

    runtime: Path
    allow_ips: set[str]  # 已归一化为 str 表示 (便于调试输出)
    system_commands: list[str]
    exclude_scan_paths: list[str]
    catalog_enable_test: bool

    def is_allowed_ip(self, ip: str) -> bool:
        """ip 命中白名单任一 (单机或网段). 白名单空则全拒."""
        if not self.allow_nets:
            return False
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        return any(addr in net for net in self.allow_nets)

    @property
    def allow_nets(self) -> list[ipaddress._BaseNetwork]:
        nets = [ipaddress.ip_network(entry, strict=False) for entry in self.allow_ips]
        nets.append(ipaddress.ip_network(f"{ipaddress.ip_address('127.0.0.1')}/32"))
        return nets
