"""HTTP 服务器，统一接收各类事件。

POST /event
  单条事件: {"event_type": "...", "event_name": "...", "description": "..."}
  批量事件: [{"event_type": "...", ...}, ...]
  动作事件: {"event_type": "action", "actions": [{"event_name": "waving", "description": "小明在挥手"}, ...]}
"""

import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

from events.buffer import EventBuffer, UnifiedEvent
from logging_config import get_logger

logger = get_logger(__name__)


class EventServer:
    """统一 HTTP 服务器，接收各类事件并写入 EventBuffer。"""

    def __init__(self, port: int, event_buffer: EventBuffer):
        self._port = port
        self._buffer = event_buffer
        self._server: HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        buf = self._buffer

        class Handler(BaseHTTPRequestHandler):
            def do_POST(handler_self):
                if handler_self.path != "/event":
                    handler_self.send_response(404)
                    handler_self.end_headers()
                    return

                content_length = int(handler_self.headers.get("Content-Length", 0))
                body = handler_self.rfile.read(content_length)
                try:
                    data = json.loads(body)

                    # 批量事件：顶层数组，单条事件：对象
                    if isinstance(data, list):
                        items = data
                    else:
                        items = [data]

                    count = 0
                    for item in items:
                        event_type = item.get("event_type", "")
                        if event_type == "action":
                            _handle_action(item, buf)
                        else:
                            _handle_generic(item, buf)
                        count += 1

                    handler_self.send_response(200)
                    handler_self.send_header("Content-Type", "application/json")
                    handler_self.end_headers()
                    handler_self.wfile.write(json.dumps({"status": "ok", "count": count}).encode())
                except Exception as e:
                    logger.error(f"[event_server] 解析失败: {e}")
                    handler_self.send_response(400)
                    handler_self.end_headers()
                    handler_self.wfile.write(json.dumps({"error": str(e)}).encode())

            def log_message(handler_self, format, *args):
                logger.debug(f"[event_server] {format % args}")

        self._server = HTTPServer(("0.0.0.0", self._port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        logger.info(f"[event_server] 已启动，端口 {self._port}")

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server = None
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        logger.info("[event_server] 已停止")


def _handle_generic(data: dict, buf: EventBuffer):
    """处理 action 之外的所有事件类型。"""
    event_name = data.get("event_name", "")
    description = data.get("description", "")
    if not description and not event_name:
        return
    event = UnifiedEvent(
        event_type=data.get("event_type", ""),
        event_name=event_name,
        description=description,
        image_base64=data.get("image_base64", ""),
        image_mime_type=data.get("image_mime_type", "image/jpeg"),
        video_base64=data.get("video_base64", ""),
        video_mime_type=data.get("video_mime_type", "video/avi"),
    )
    buf.put(event)


def _handle_action(data: dict, buf: EventBuffer):
    actions = data.get("actions")
    if actions:
        for item in actions:
            _put_action(item, buf)
    else:
        _put_action(data, buf)


def _put_action(item: dict, buf: EventBuffer):
    event_name = item.get("event_name", "")
    description = item.get("description", "")
    if not description:
        logger.warning(f"[event_server] 缺少 description")
        return
    event = UnifiedEvent(
        event_type="action",
        event_name=event_name,
        description=description,
        image_base64=item.get("image_base64", ""),
        image_mime_type=item.get("image_mime_type", "image/jpeg"),
        video_base64=item.get("video_base64", ""),
        video_mime_type=item.get("video_mime_type", "video/avi"),
    )
    buf.put(event)