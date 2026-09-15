"""外部网络服务客户端库 (搜索/天气/时间).

端点 web.iqs_endpoint 读 /config/config.yaml, 密钥 IQS_API_KEY 读 /config/.env
(CMD_CONFIG 可覆盖根). 无硬编码密钥, 缺 key 报错.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import socket
import urllib.error
import urllib.request

import yaml


def _config_root() -> Path:
    return Path(os.environ.get("CMD_CONFIG", "/config"))


def _load_env() -> str:
    env_file = _config_root() / ".env"
    if not env_file.exists():
        return ""
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("IQS_API_KEY="):
            return line.partition("=")[2].strip().strip('"').strip("'")
    return ""


def _iqs_endpoint() -> str:
    data = yaml.safe_load((_config_root() / "config.yaml").read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        return "https://cloud-iqs.aliyuncs.com/search/unified"
    return str((data.get("web", {}) or {}).get("iqs_endpoint", "https://cloud-iqs.aliyuncs.com/search/unified"))


def _iqs_api_key() -> str:
    key = _load_env().strip()
    if not key:
        raise RuntimeError("缺少 IQS_API_KEY (在 cmd_server/config/.env 配置)")
    return key


def _call_iqs(payload: dict) -> dict:
    req = urllib.request.Request(
        _iqs_endpoint(),
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {_iqs_api_key()}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    # 强制 IPv4 (容器无 IPv6 路由)
    orig = socket.getaddrinfo
    socket.getaddrinfo = lambda h, p, family=0, *a, **kw: orig(h, p, socket.AF_INET, *a, **kw)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        raise RuntimeError(f"IQS API 返回错误 {e.code}: {body}") from e
    finally:
        socket.getaddrinfo = orig


def search_web(query: str) -> str:
    """联网搜索, 返回搜索结果摘要."""
    payload = {
        "query": query,
        "engineType": "LiteAdvanced",
        "contents": {"mainText": False, "summary": False, "rerankScore": True},
        "advancedParams": {"numResults": "5"},
    }
    result = _call_iqs(payload)
    page_items = result.get("pageItems", [])
    if not page_items:
        return f'未找到与"{query}"相关的搜索结果'
    lines = [f'搜索"{query}"的结果:']
    for i, item in enumerate(page_items, 1):
        title = item.get("title", "无标题")
        snippet = item.get("snippet", "")
        link = item.get("link", "")
        if len(snippet) > 300:
            snippet = snippet[:300] + "..."
        lines.append(f"\n{i}. {title}\n   {snippet}\n   {link}")
    return "\n".join(lines)


def get_weather(city: str) -> str:
    """查询指定城市的实时天气和未来预报."""
    payload = {"query": f"{city}天气", "engineType": "Generic", "locationInfo": {"city": city}}
    result = _call_iqs(payload)
    weather_item = next(
        (
            json.loads(item.get("detail", "{}"))
            for item in result.get("sceneItems", [])
            if item.get("type") == "weather"
        ),
        None,
    )
    if not weather_item:
        return f"未找到{city}的天气信息"
    loc = weather_item.get("location", {})
    realtime = weather_item.get("realtimeData", {})
    daily = weather_item.get("weatherForecastData", {}).get("dailyForecast", [])
    district = loc.get("district", city)
    lines = [
        f"{district}天气",
        f"当前: {realtime.get('weather', 'N/A')}，温度 {realtime.get('temp', 'N/A')}℃，"
        f"湿度 {realtime.get('humidity', 'N/A')}%",
    ]
    if realtime.get("windDir"):
        lines.append(f"风: {realtime['windDir']}{realtime.get('windLevel', '')}级")
    if realtime.get("tips"):
        lines.append(f"提示: {realtime['tips']}")
    if daily := daily[:7]:
        lines.append("未来预报:")
        for d in daily:
            lines.append(
                f"  {d.get('predictDate', '')}: {d.get('weatherDay', '')}，{d.get('tempLow', '')}~{d.get('tempHigh', '')}℃"
            )
    return "\n".join(lines)


def get_current_time() -> str:
    """联网获取当前时间, 失败回退本地 Asia/Shanghai."""
    try:
        req = urllib.request.Request(
            "http://worldtimeapi.org/api/timezone/Asia/Shanghai", headers={"User-Agent": "robot-cli"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        dt = datetime.fromisoformat(data["datetime"])
        return dt.strftime("%Y年%m月%d日 %H:%M:%S")
    except Exception:
        try:
            from zoneinfo import ZoneInfo

            now = datetime.now(ZoneInfo("Asia/Shanghai"))
        except Exception:
            now = datetime.now()
        return now.strftime("%Y年%m月%d日 %H:%M:%S")
