import asyncio
import json
import logging
import struct
import tempfile
import time
from typing import Optional

from rosbags.typesys import Stores
from rosbags.typesys import get_types_from_msg
from rosbags.typesys import get_typestore
import websockets

OPCODE_MESSAGE_DATA = 0x01
BINARY_HEADER_SIZE = 13
_HEADER_STRUCT = struct.Struct("<xIQ")
SUBPROTOCOL = "foxglove.sdk.v1"

logger = logging.getLogger(__name__)


def _parse_foxglove_schema(schema_text, root_schema_name):
    parts = schema_text.split("\n===\nMSG: ")
    all_types = {}
    root_text = parts[0].strip()
    if root_text:
        try:
            all_types.update(get_types_from_msg(root_text, root_schema_name))
        except Exception as e:
            logger.debug("schema parse failed %s: %s", root_schema_name, e)
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
                logger.warning("schema parse failed %s: %s", type_name, e)
    return all_types


class _FoxgloveClient:
    def __init__(self, url):
        self._url = url
        self._typestore = get_typestore(Stores.ROS2_JAZZY)
        self._ws = None
        self._channels = {}
        self._topic_to_channel = {}
        self._subs = {}
        self._next_sub_id = 1

    async def connect(self, timeout=3.0):
        self._ws = await websockets.connect(
            self._url,
            subprotocols=[SUBPROTOCOL],
            max_size=None,
            open_timeout=5,
        )
        await self._discover(timeout)

    async def close(self):
        if self._ws:
            await self._ws.close()
            self._ws = None

    @property
    def topics(self):
        return sorted(self._topic_to_channel.keys())

    async def subscribe(self, topic):
        channel_id = self._topic_to_channel[topic]
        sub_id = self._next_sub_id
        self._next_sub_id += 1
        self._subs[sub_id] = channel_id
        await self._ws.send(
            json.dumps(
                {
                    "op": "subscribe",
                    "subscriptions": [{"id": sub_id, "channelId": channel_id}],
                }
            )
        )
        return sub_id

    async def unsubscribe(self, sub_id):
        self._subs.pop(sub_id, None)
        await self._ws.send(
            json.dumps(
                {
                    "op": "unsubscribe",
                    "subscriptionIds": [sub_id],
                }
            )
        )

    async def recv_message(self, timeout=5.0):
        frame = await self._recv_frame(timeout)
        if frame is None:
            return None
        sub_id, timestamp_ns, payload = frame
        info = self._channels.get(self._subs[sub_id])
        if not info:
            return None
        msg = self._typestore.deserialize_cdr(payload, info["schemaName"])
        return info["topic"], msg, timestamp_ns

    async def _recv_frame(self, timeout):
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

    async def _discover(self, timeout):
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
            if op == "advertise":
                for ch in data["channels"]:
                    self._channels[ch["id"]] = ch
                    self._topic_to_channel[ch["topic"]] = ch["id"]
                    self._register_schema(ch)
                got_channels = True
            elif op == "advertiseServices":
                if got_channels:
                    break

    def _register_schema(self, channel):
        schema_name = channel["schemaName"]
        if schema_name in self._typestore.types:
            return
        schema_text = channel.get("schema", "")
        if not schema_text:
            return
        types = _parse_foxglove_schema(schema_text, schema_name)
        new_types = {k: v for k, v in types.items() if k not in self._typestore.types}
        if new_types:
            try:
                self._typestore.register(new_types)
            except Exception as e:
                logger.warning("register type failed %s: %s", schema_name, e)


def capture_ros_frame(ws_url: str, topic: str, timeout: float = 8.0) -> Optional[str]:
    """从 Foxglove WebSocket 捕获一帧摄像头画面，保存为临时 JPEG 文件，返回路径"""
    client = _FoxgloveClient(ws_url)
    result_path: Optional[str] = None

    async def _capture():
        nonlocal result_path
        try:
            await client.connect()
            if topic not in client.topics:
                print(f"[camera] 话题不存在: {topic}", flush=True)
                return
            sub_id = await client.subscribe(topic)
            try:
                skip = 2
                while True:
                    result = await client.recv_message(timeout=timeout)
                    if result is None:
                        print("[camera] 接收摄像头帧超时", flush=True)
                        return
                    t, msg, _ = result
                    if t != topic:
                        continue
                    if skip > 0:
                        skip -= 1
                        continue
                    raw_data = bytes(msg.data)
                    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".jpg")
                    tmp.write(raw_data)
                    tmp.close()
                    result_path = tmp.name
                    return
            finally:
                await client.unsubscribe(sub_id)
        except Exception as e:
            print(f"[camera] 摄像头捕获失败: {e}", flush=True)
        finally:
            try:
                await client.close()
            except Exception as e:
                # close() 在断连/异常 id 下可能抛, 不影响已捕获结果, 吞掉即可
                print(f"[camera] 关闭连接异常(忽略): {e}", flush=True)

    asyncio.run(_capture())
    return result_path
