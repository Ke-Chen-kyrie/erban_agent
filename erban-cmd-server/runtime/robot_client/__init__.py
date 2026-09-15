"""机器人 WS 客户端库.

命令模块共用: 连接机器人 WebSocket, 发 JSON 命令, 统一成功/失败退出码.
连接参数是本库配置, 由库自读 /config/config.yaml 的 robot 段 (CMD_CONFIG 可覆盖根).
导航站位距离配置读同一配置文件的 navigation 段 (navigate 用).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any, Optional

import websocket
import yaml

_DEFAULT_HOST = "192.168.217.253"
_DEFAULT_PORT = 9092
_DEFAULT_TIMEOUT = 180.0


def _config_root() -> Path:
    return Path(os.environ.get("CMD_CONFIG", "/config"))


def _robot_section() -> dict:
    cfg_path = _config_root() / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    if not isinstance(data, dict):
        return {}
    return data.get("robot", {}) or {}


def robot_host() -> str:
    return str(_robot_section().get("host", _DEFAULT_HOST))


def robot_port() -> int:
    return int(_robot_section().get("port", _DEFAULT_PORT))


def robot_timeout() -> float:
    return float(_robot_section().get("timeout", _DEFAULT_TIMEOUT))


# --- navigation 段: 站位距离 (navigate 用) ---


def _navigation_section() -> dict:
    cfg_path = _config_root() / "config.yaml"
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
    if not isinstance(data, dict):
        return {}
    return data.get("navigation", {}) or {}


def navigation_standoff_distance() -> float:
    """导航到目标用户前方时的默认站位距离 (米), 缺省 0.6."""
    return float(_navigation_section().get("standoff_distance", 0.6))


class RobotClient:
    """机器人客户端: 连接 + 发送 JSON 命令 + 断开."""

    def __init__(self, host: Optional[str] = None, port: Optional[int] = None, timeout: Optional[float] = None):
        self.host = host or robot_host()
        self.port = port or robot_port()
        self.timeout = timeout or robot_timeout()
        self._ws: Optional[websocket.WebSocket] = None

    def connect(self) -> bool:
        ws_url = f"ws://{self.host}:{self.port}"
        try:
            self._ws = websocket.create_connection(ws_url, timeout=self.timeout)
            return True
        except Exception as e:
            print(f"连接机器人失败: {e}", file=sys.stderr)
            return False

    def disconnect(self) -> None:
        if self._ws:
            self._ws.close()
            self._ws = None

    def send_command(self, command: dict[str, Any], recv_timeout: Optional[float] = None) -> Optional[dict[str, Any]]:
        if not self._ws:
            print("未连接机器人", file=sys.stderr)
            return None
        try:
            self._ws.send(json.dumps(command))
            self._ws.settimeout(recv_timeout or self.timeout)
            return json.loads(self._ws.recv())
        except Exception as e:
            print(f"命令执行失败: {e}", file=sys.stderr)
            return None


def run_robot_command(command: dict[str, Any], label: str) -> None:
    """发单条机器人命令: 成功打印 message 退 0, 失败退 1.

    供各命令 .py 复用 (Rule of Three), 参数校验留在各自 argparse.
    """
    client = RobotClient()
    if not client.connect():
        sys.exit(1)
    result = client.send_command(command)
    client.disconnect()
    if result and result.get("success", True):
        print(result.get("message", f"{label}成功"))
        return
    msg = result.get("message", "失败") if result else "无响应"
    print(f"{label}失败: {msg}", file=sys.stderr)
    sys.exit(1)

# --- Langfuse 参观讲解词 (仿 proactive_agent_realtime 配置: .env 密钥 + cert 验证) ---


def _langfuse_env() -> dict:
    """读 cmd_server/config/.env 的 langfuse 配置."""
    env_file = Path(os.environ.get("CMD_CONFIG", "/config")) / ".env"
    cfg = {"base": "", "pk": "", "sk": ""}
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            v = v.strip().strip('"').strip("'")
            if k == "LANGFUSE_BASE_URL":
                cfg["base"] = v
            elif k == "LANGFUSE_PUBLIC_KEY":
                cfg["pk"] = v
            elif k == "LANGFUSE_SECRET_KEY":
                cfg["sk"] = v
    if not (cfg["base"] and cfg["pk"] and cfg["sk"]):
        raise RuntimeError("缺少 langfuse 配置 (LANGFUSE_BASE_URL/PUBLIC_KEY/SECRET_KEY), 在 cmd_server/config/.env")
    return cfg


def fetch_ep_material(ep: str) -> str:
    """从 Langfuse 拉参观讲解词 visitroom_materails, 返回指定 EP (EP1~EP5) 的播报词.

    仿 proactive_agent_realtime: httpx + certs/langfuse-erban-cert.pem 自签名验证.
    """
    import base64
    import json
    import httpx

    env = _langfuse_env()
    url = f"{env['base'].rstrip('/')}/api/public/v2/prompts/proactive_agent_realtime%2Fvisitroom_materails?label=production"
    token = base64.b64encode(f"{env['pk']}:{env['sk']}".encode("utf-8")).decode("ascii")
    headers = {"Authorization": f"Basic {token}"}
    cert = Path(__file__).resolve().parent / "certs" / "langfuse-erban-cert.pem"
    kwargs = {"verify": str(cert)} if cert.exists() else {}
    with httpx.Client(**kwargs) as client:
        resp = client.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        raw = resp.json()["prompt"]  # prompt 字段为该讲解词的 JSON 字符串
    data = json.loads(raw, strict=False)  # langfuse 编辑器会在长文本中插入软换行
    ep = ep.upper()
    item = data.get("episodes", {}).get(ep)
    if not item:
        raise ValueError(f"未知 EP: {ep}, 可选: {', '.join(data.get('episodes', {}).keys())}")
    import re
    return re.sub(r"\s+", " ", item.get("broadcast", "")).strip()