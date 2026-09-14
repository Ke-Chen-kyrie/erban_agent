import json
import logging
from typing import Any, Optional
import websocket

logger = logging.getLogger(__name__)


class RobotClient:
    """机器人客户端 - 通过 WebSocket 发送 JSON 命令"""

    def __init__(self, host: str = "192.168.217.253", port: int = 9092, timeout: float = 30.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._ws: Optional[websocket.WebSocket] = None

    def connect(self) -> bool:
        ws_url = f"ws://{self.host}:{self.port}"
        try:
            self._ws = websocket.create_connection(ws_url, timeout=self.timeout)
            logger.info(f"Connected to {ws_url}")
            return True
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            return False

    def disconnect(self):
        if self._ws:
            self._ws.close()
            self._ws = None

    def send_command(self, command: dict[str, Any], recv_timeout: float = 180.0) -> Optional[dict[str, Any]]:
        if not self._ws:
            logger.error("Not connected")
            return None
        try:
            self._ws.send(json.dumps(command))
            self._ws.settimeout(recv_timeout)
            response = json.loads(self._ws.recv())
            logger.info(f"Command: {command}, Response: {response}")
            return response
        except Exception as e:
            logger.error(f"Command error: {e}")
            return None