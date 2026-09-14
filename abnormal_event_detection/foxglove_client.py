"""Foxglove WebSocket 客户端 —— 不依赖 ROS2 运行时的话题订阅与图像反序列化."""

import asyncio
import json
import logging
import struct
import threading
import time
from typing import Optional, Tuple

import numpy as np
import websockets
from rosbags.typesys import Stores, get_typestore, get_types_from_msg
from logging_config import get_logger
from config import FOXGLOVE_BRIDGE_URL
SUBPROTOCOL = "foxglove.sdk.v1"
OPCODE_MESSAGE_DATA = 0x01
BINARY_HEADER_SIZE = 13
_HEADER_STRUCT = struct.Struct("<xIQ")

logger = get_logger(__name__)


def parse_foxglove_schema(schema_text: str, root_schema_name: str) -> dict:
    """从 foxglove advertise 中的拼接 schema 文本解析出 rosbags 类型定义."""
    parts = schema_text.split("\n===\nMSG: ")
    all_types = {}

    root_text = parts[0].strip()
    if root_text:
        try:
            all_types.update(get_types_from_msg(root_text, root_schema_name))
        except Exception as e:
            logger.debug("schema 解析失败 %s: %s", root_schema_name, e)

    for part in parts[1:]:
        lines = part.strip().split("\n")
        if not lines:
            continue
        type_name = lines[0].strip()
        msg_text = "\n".join(lines[1:]).strip()
        if msg_text:
            try:
                all_types.update(get_types_from_msg(msg_text, type_name))
            except Exception as e:
                logger.debug("schema 解析失败 %s: %s", type_name, e)

    return all_types


class FoxgloveClient:
    """Foxglove WebSocket 客户端."""

    def __init__(self, url: str = None):
        self._url = url or FOXGLOVE_BRIDGE_URL
        self._typestore = get_typestore(Stores.ROS2_JAZZY)
        self._ws = None
        self._channels: dict = {}
        self._topic_to_channel: dict = {}
        self._services: dict = {}
        self._subs: dict = {}
        self._next_sub_id = 1

    async def connect(self, discovery_timeout: float = 3.0):
        """连接 bridge 并完成 channel/service 发现和 schema 注册."""
        self._ws = await websockets.connect(
            self._url,
            subprotocols=[SUBPROTOCOL],
            max_size=None,
            open_timeout=5,
        )
        await self._discover(discovery_timeout)

    async def close(self):
        if self._ws:
            await self._ws.close()
            self._ws = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.close()

    @property
    def channels(self):
        return self._channels

    @property
    def topics(self):
        return sorted(self._topic_to_channel.keys())

    def get_channel(self, topic: str):
        channel_id = self._topic_to_channel.get(topic)
        if channel_id is None:
            return None
        return self._channels.get(channel_id)

    async def subscribe(self, topic: str) -> int:
        """订阅话题，返回 sub_id."""
        channel_id = self._topic_to_channel[topic]
        sub_id = self._next_sub_id
        self._next_sub_id += 1
        self._subs[sub_id] = channel_id
        await self._ws.send(json.dumps({
            "op": "subscribe",
            "subscriptions": [{"id": sub_id, "channelId": channel_id}],
        }))
        return sub_id

    async def unsubscribe(self, sub_id: int):
        """取消订阅."""
        self._subs.pop(sub_id, None)
        await self._ws.send(json.dumps({
            "op": "unsubscribe",
            "subscriptionIds": [sub_id],
        }))

    async def recv_message(self, timeout: float = 5.0):
        """接收下一条已订阅话题的消息，返回 (topic, msg, timestamp_ns)."""
        frame = await self._recv_frame(timeout)
        if frame is None:
            return None
        sub_id, timestamp_ns, payload = frame
        info = self._channels.get(self._subs[sub_id])
        if not info:
            return None
        msg = self._typestore.deserialize_cdr(payload, info["schemaName"])
        return info["topic"], msg, timestamp_ns

    async def _recv_frame(self, timeout: float):
        """接收下一个已订阅的二进制数据帧."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                return None
            if not isinstance(raw, bytes) or len(raw) < BINARY_HEADER_SIZE or raw[0] != OPCODE_MESSAGE_DATA:
                continue
            sub_id, timestamp_ns = _HEADER_STRUCT.unpack_from(raw)
            if sub_id not in self._subs:
                continue
            return sub_id, timestamp_ns, raw[BINARY_HEADER_SIZE:]
        return None

    async def _discover(self, timeout: float):
        """接收 advertise 和 advertiseServices，注册所有 schema."""
        deadline = time.monotonic() + timeout
        got_channels = False

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            wait = min(remaining, 0.5) if got_channels else remaining
            try:
                raw = await asyncio.wait_for(self._ws.recv(), timeout=wait)
            except asyncio.TimeoutError:
                if got_channels:
                    break
                continue

            if not isinstance(raw, str):
                continue
            data = json.loads(raw)
            op = data.get("op")

            if op == "serverInfo":
                logger.debug("服务端: %s", data.get("name"))
            elif op == "advertise":
                for ch in data["channels"]:
                    self._channels[ch["id"]] = ch
                    self._topic_to_channel[ch["topic"]] = ch["id"]
                    self._register_schema(ch)
                    ch.pop("schema", None)
                got_channels = True
            elif op == "advertiseServices":
                for svc in data.get("services", []):
                    self._services[svc["id"]] = svc
                if got_channels:
                    break

        logger.debug("发现 %d 个话题", len(self._channels))

    def _register_schema(self, channel: dict):
        """从 channel 的 schema 文本注册类型到 typestore."""
        schema_name = channel["schemaName"]
        if schema_name in self._typestore.types:
            return

        schema_text = channel.get("schema", "")
        if not schema_text:
            return

        types = parse_foxglove_schema(schema_text, schema_name)
        new_types = {k: v for k, v in types.items() if k not in self._typestore.types}
        if new_types:
            try:
                self._typestore.register(new_types)
            except Exception as e:
                logger.warning("注册类型失败 %s: %s", schema_name, e)


def decode_ros_image(msg) -> Optional[np.ndarray]:
    """解码 ROS2 图像消息为 RGB numpy 数组."""
    import cv2

    try:
        # CompressedImage
        if hasattr(msg, "format") and hasattr(msg, "data"):
            np_arr = np.frombuffer(msg.data, np.uint8)
            img = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
            if img is not None:
                return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            return None

        # Raw Image
        if hasattr(msg, "encoding") and hasattr(msg, "data"):
            img = np.frombuffer(msg.data, np.uint8).reshape(
                msg.height, msg.width, -1
            )
            encoding = msg.encoding
            if encoding == "rgb8":
                return img
            elif encoding == "bgr8":
                return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            elif encoding == "mono8":
                return cv2.cvtColor(img, cv2.COLOR_GRAY2RGB)
            else:
                return img if img.shape[2] == 3 else None

        return None
    except Exception as e:
        logger.error(f"[foxglove] 图像解码失败: {e}")
        return None


class FoxgloveImageCapture:
    """从 Foxglove 话题读取最新一帧图像（单帧捕获，适合工具调用场景）.

    使用方式:
        cap = FoxgloveImageCapture("/camera/image/compressed")
        success, img = cap.read()  # img 是 RGB numpy 数组
        cap.release()
    """

    def __init__(self, topic: str, url: str = None):
        self._topic = topic
        self._url = url or FOXGLOVE_BRIDGE_URL
        self._client = FoxgloveClient(url)
        self._sub_id = None
        self._latest_frame: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

    def read(self, timeout: float = 1.0) -> Tuple[bool, Optional[np.ndarray]]:
        """读取最新一帧图像，返回 (success, image)."""
        if not self._running:
            self._connect_sync()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._latest_frame is not None:
                    return True, self._latest_frame.copy()
            time.sleep(0.01)

        return False, None

    def release(self):
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.release()

    def _connect_sync(self):
        self._running = True
        self._thread = threading.Thread(target=self._run_async_loop, daemon=True)
        self._thread.start()
        for _ in range(50):
            if self._sub_id is not None:
                break
            time.sleep(0.1)
        if self._sub_id is None:
            logger.error(f"[foxglove] 订阅超时: 话题 '{self._topic}' 5秒内未收到帧")

    def _run_async_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self._async_main())
        loop.close()

    async def _async_main(self):
        try:
            await self._client.connect()
            if self._topic not in self._client.topics:
                logger.error(f"[foxglove] 话题不存在: {self._topic}")
                self._running = False
                return

            self._sub_id = await self._client.subscribe(self._topic)

            while self._running:
                result = await self._client.recv_message(timeout=0.1)
                if result is None:
                    continue
                topic, msg, _ = result
                if topic != self._topic:
                    continue

                img = decode_ros_image(msg)
                if img is not None:
                    with self._lock:
                        self._latest_frame = img

        except Exception as e:
            logger.error(f"[foxglove] 错误: {e}")
        finally:
            if self._sub_id:
                await self._client.unsubscribe(self._sub_id)
            await self._client.close()
            self._running = False