from datetime import datetime
from dotenv import load_dotenv

import json
import os

load_dotenv()
import socket
import subprocess
import urllib.error
import urllib.request


def run_shell(cmd: str) -> str:
    """执行 Shell 命令并返回 stdout + stderr"""
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True, timeout=600, env=os.environ.copy()
        )
        output = result.stdout
        if result.stderr:
            output += result.stderr
        return output.rstrip("\n")
    except subprocess.TimeoutExpired:
        return "命令执行超时"
    except Exception as e:
        return f"命令执行异常: {e}"


def env() -> dict:
    """读取所有配置，环境变量优先，否则使用默认值"""
    return {
        "host": os.getenv("ROBOT_HOST", "192.168.217.100"),
        "port": int(os.getenv("ROBOT_PORT", "9092")),
        "timeout": float(os.getenv("ROBOT_TIMEOUT", "180.0")),
        "camera_ws_url": os.getenv("CAMERA_WS_URL", "ws://192.168.217.100:8768"),
        "camera_topic": os.getenv(
            "CAMERA_TOPIC",
            "/zj_humanoid/sensor/realsense_head/color/image_raw/compressed",
        ),
        "identification_url": os.getenv(
            "IDENTIFICATION_URL", "http://192.168.217.100:8001"
        ),
        "iqs_api_key": os.getenv(
            "IQS_API_KEY", "Q3OC-r7BNaWfoIlR9mbBF-Y_P-4o8YQ2YTE4ZmEwMQ"
        ),
        "iqs_endpoint": os.getenv(
            "IQS_ENDPOINT", "https://cloud-iqs.aliyuncs.com/search/unified"
        ),
    }


def call_iqs(payload: dict, conf: dict | None = None) -> dict:
    """调用阿里云 IQS 统一搜索 API"""
    if conf is None:
        conf = env()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        conf["iqs_endpoint"],
        data=data,
        headers={
            "Authorization": f"Bearer {conf['iqs_api_key']}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    # 强制 IPv4（容器无 IPv6 路由）
    orig = socket.getaddrinfo
    socket.getaddrinfo = lambda h, p, family=0, *a, **kw: orig(h, p, socket.AF_INET, *a, **kw)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        raise RuntimeError(f"IQS API 返回错误 {e.code}: {body}")
    finally:
        socket.getaddrinfo = orig


def search_web(query: str) -> str:
    """联网搜索，返回搜索结果摘要"""
    payload = {
        "query": query,
        "engineType": "LiteAdvanced",
        "contents": {
            "mainText": False,
            "summary": False,
            "rerankScore": True,
        },
        "advancedParams": {
            "numResults": "5",
        },
    }
    result = call_iqs(payload)
    page_items = result.get("pageItems", [])
    if not page_items:
        return f"未找到与\"{query}\"相关的搜索结果"

    lines = [f"搜索\"{query}\"的结果:"]
    for i, item in enumerate(page_items, 1):
        title = item.get("title", "无标题")
        snippet = item.get("snippet", "")
        link = item.get("link", "")
        if len(snippet) > 300:
            snippet = snippet[:300] + "..."
        lines.append(f"\n{i}. {title}\n   {snippet}\n   {link}")

    return "\n".join(lines)


def get_weather(city: str) -> str:
    """查询指定城市的实时天气和未来预报"""
    payload = {
        "query": f"{city}天气",
        "engineType": "Generic",
        "locationInfo": {
            "city": city,
        },
    }
    result = call_iqs(payload)
    scene_items = result.get("sceneItems", [])
    weather_item = None
    for item in scene_items:
        if item.get("type") == "weather":
            weather_item = json.loads(item.get("detail", "{}"))
            break

    if not weather_item:
        return f"未找到{city}的天气信息"

    loc = weather_item.get("location", {})
    realtime = weather_item.get("realtimeData", {})
    forecast = weather_item.get("weatherForecastData", {})
    daily = forecast.get("dailyForecast", [])

    district = loc.get("district", city)
    lines = [
        f"{district}天气",
        f"当前: {realtime.get('weather', 'N/A')}，温度 {realtime.get('temp', 'N/A')}℃，"
        f"湿度 {realtime.get('humidity', 'N/A')}%，"
        f"{realtime.get('windDir', '')}{realtime.get('windLevel', '')}级",
    ]
    if realtime.get("tips"):
        lines.append(f"提示: {realtime['tips']}")

    if daily:
        lines.append("\n未来预报:")
        for d in daily[:7]:
            lines.append(
                f"  {d.get('predictDate', '')}: {d.get('weatherDay', '')}，"
                f"{d.get('tempLow', '')}~{d.get('tempHigh', '')}℃"
            )

    return "\n".join(lines)


def get_current_time() -> str:
    """联网获取当前日期和时间，失败时回退到本地时间（Asia/Shanghai）"""
    try:
        req = urllib.request.Request(
            "http://worldtimeapi.org/api/timezone/Asia/Shanghai",
            headers={"User-Agent": "robot-cli"},
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