#!/usr/bin/env python3
"""Seeduplex「唱歌工具」—— 豆包实时语音模型 3.0（Seeduplex）。

自包含单文件（一个 SingTool 类 + Linux 风格 CLI）。只依赖 websockets / sounddevice /
numpy / scipy / tomllib（Python<3.11 用 tomli）。

原理：把唱歌需求写进 session 的 instructions（系统提示词）并开启 enable_proactive_speak，
再把预合成好的起拍语音（默认 trigger.wav）喂回作"开始唱吧"触发，模型即按提示词演唱。

先合成起拍语音（一次即可）：
    sing --make-trigger                    # 生成 trigger.wav（默认"开始唱吧"）
    sing --make-trigger --trigger-phrase "请开始吧"

再唱歌：
    sing "唱一首温柔甜美的生日歌"
    sing My Heart Will Go On               # 英文歌名含空格可不加引号（多个词自动拼接）
    sing --voice zh_female_xiaohe_jupiter_bigtts "唱一首新年歌"
    sing --no-play "唱一首歌"              # 只返回摘要，不播放（干跑）

Python 用法：
    from sing_tool import SingTool, sing, TOOL_SCHEMA
    SingTool().sing("唱一首温柔的生日歌")   # 默认读 sing_tool.py 同目录的 config.toml
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import queue
import sys
import threading
import uuid
import wave
from typing import Any, Dict, Optional

import numpy as np
import sounddevice as sd
import websockets
from scipy.signal import resample_poly

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

__version__ = "0.2.0"

ENDPOINT = "wss://openspeech.bytedance.com/api/v3/duplex/realtime/dialogue"
MODEL = "1.2.6.1"
OUTPUT_RATE = 24000
INPUT_RATE = 16000
DEFAULT_APP_KEY = "PlgvMymc7f3tQnJ6"  # 旧版控制台固定公共网关 key
DEFAULT_VOICE = "zh_female_vv_jupiter_bigtts"
DEFAULT_REQUIREMENT = "唱一首温柔甜美的生日歌"  # 省略需求时的默认曲目
TRIGGER_PHRASE = "开始唱吧"           # 起拍语音文本（--make-trigger 用）

# 默认文件（config.toml / trigger.wav）固定解析到 sing_tool.py 所在目录，保证从任意 cwd 调用都能找到。
# 本项目按 editable 方式安装（pip install -e .），__file__ 即源码目录 sing/。
_MODULE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(_MODULE_DIR, "config.toml")
TRIGGER_FILE = os.path.join(_MODULE_DIR, "trigger.wav")   # 起拍语音文件（运行时读取 / --make-trigger 写出）

# agent function-calling 工具描述
TOOL_SCHEMA = {
    "type": "function",
    "name": "sing",
    "description": "根据唱歌需求描述，让豆包实时语音模型(Seeduplex)现场唱一段并直接通过扬声器播放，返回唱歌摘要（歌词与时长）。",
    "parameters": {
        "type": "object",
        "properties": {
            "requirement": {
                "type": "string",
                "description": "唱歌需求描述，例如 '唱一首温柔甜美的生日歌'",
            }
        },
        "required": ["requirement"],
    },
}


def _int_or_none(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_cfg(path: str) -> Dict[str, Any]:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    auth = data.get("auth") or {}
    sess = data.get("session") or {}
    dev = data.get("device") or {}
    return {
        "api_key": auth.get("api_key", ""),           # Access Token（X-Api-Access-Key）
        "app_id": auth.get("app_id", ""),             # App ID（X-Api-App-ID）
        "app_key": auth.get("app_key", DEFAULT_APP_KEY),
        "resource_id": auth.get("resource_id", "volc.speech.dialog"),
        "voice": sess.get("speaker", DEFAULT_VOICE),
        "enable_music": bool(sess.get("enable_music", True)),
        "speaker_name": os.getenv("SPEAKER_DEVICE_NAME") or dev.get("speaker_name") or "",
        "speaker_index": _int_or_none(os.getenv("SPEAKER_DEVICE_INDEX") or dev.get("speaker_index")),
    }


def _decode_audio(delta: str) -> bytes:
    try:
        return base64.b64decode(delta or "")
    except Exception:
        return b""


def _resample_s16le(data: bytes, src_rate: int, dst_rate: int) -> bytes:
    """int16 单声道重采样（scipy resample_poly，float32 精度），src_rate -> dst_rate。"""
    if not data:
        return b""
    audio = np.frombuffer(data, dtype=np.int16).astype(np.float32)
    if src_rate != dst_rate:
        audio = resample_poly(audio, dst_rate, src_rate)
    return audio.astype(np.int16).tobytes()


def _load_trigger_wav(path: str) -> bytes:
    """读取起拍语音 wav，返回 16k int16 单声道 raw 字节。"""
    with wave.open(path, "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        data = w.readframes(w.getnframes())
    if channels != 1 or width != 2:
        raise ValueError(f"起拍语音需为单声道 16bit PCM，实际 channels={channels} width={width}")
    if rate != INPUT_RATE:
        data = _resample_s16le(data, rate, INPUT_RATE)
    return data


_END = object()  # 播放队列哨兵


def _find_sd_device_by_name(keyword: str, kind: str = "output") -> Optional[int]:
    """按名称关键词在 sounddevice 中查找设备（大小写不敏感子串匹配）。"""
    for i, dev in enumerate(sd.query_devices()):
        if keyword.lower() in dev["name"].lower():
            if kind == "output" and dev["max_output_channels"] > 0:
                return i
    return None


class _StreamPlayer:
    """流式播放器：独立线程消费队列，把模型 24k int16 PCM 转 float32、按设备原生采样率重采样后送 sounddevice 输出。

    与 proactive_agent_realtime 的 AudioPlayer 保持一致：float32 dtype + resample_poly 重采样。
    播放是阻塞操作，如果放在 asyncio 事件循环里直接 stream.write()，会卡住收包，
    所以用「事件循环往队列写 → 后台线程出队播放」解耦。
    """

    def __init__(self, source_rate: int, device_index: Optional[int], device_name: str):
        self._source_rate = source_rate
        self._dtype = "float32"
        self._stream = None
        self._device = None
        self._device_rate = None

        # 设备选择优先级：名称关键词 > 显式索引 > 系统默认（与参考实现一致）
        if device_name:
            idx = _find_sd_device_by_name(device_name, kind="output")
            if idx is not None:
                device_index = idx
            else:
                print(f"找不到扬声器设备（关键词: {device_name}），使用默认设备", file=sys.stderr)
                device_name = ""
        try:
            info = sd.query_devices(device_index) if device_index is not None else sd.query_devices(kind="output")
            self._device = int(info["index"])
            self._device_rate = int(info["default_samplerate"])
            print(f"播放设备: {info['name']} (索引 {self._device}, 采样率 {self._device_rate} Hz)", file=sys.stderr)
        except Exception as exc:  # 查询失败则回退到源码率
            print(f"查询输出设备失败，回退源码率: {exc}", file=sys.stderr)

        self._queue: "queue.Queue" = queue.Queue(maxsize=900)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        out_rate = self._device_rate if self._device_rate else self._source_rate
        try:
            self._stream = self._open_stream(out_rate)
            print(f"播放采样率: {out_rate} Hz (dtype={self._dtype})", file=sys.stderr)
        except Exception as exc:  # 打不开设备：清空队列退出，避免 close() 等待一个死线程
            print(f"播放设备打开失败: {exc}", file=sys.stderr)
            self._drain_queue()
            return
        while True:
            chunk = self._queue.get()
            if chunk is _END:
                break
            try:
                self._write(chunk, out_rate)
            except Exception as exc:  # 单次写入失败（如设备被拔出）不弄垮整条流
                print(f"播放失败: {exc}", file=sys.stderr)

    def _open_stream(self, out_rate: int):
        try:
            stream = sd.OutputStream(
                samplerate=out_rate, channels=1, dtype=self._dtype, device=self._device
            )
            stream.start()
            return stream
        except Exception:
            # PortAudio/ALSA 可能处于不一致状态，重置后重试一次
            print("播放设备打开失败，重置 PortAudio 后重试", file=sys.stderr)
            try:
                sd._terminate()
                sd._initialize()
            except Exception:
                pass
            stream = sd.OutputStream(
                samplerate=out_rate, channels=1, dtype=self._dtype, device=self._device
            )
            stream.start()
            return stream

    def _drain_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def _write(self, chunk: bytes, out_rate: int) -> None:
        audio = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
        if self._source_rate != out_rate:
            audio = resample_poly(audio, out_rate, self._source_rate)
        self._stream.write(audio)  # 阻塞到此片播完，天然按实时节奏放音

    def write(self, chunk: bytes) -> None:
        # 非阻塞入队；队列满时丢弃最旧块（与参考实现的 enqueue 一致）
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(chunk)
            except queue.Full:
                pass

    def close(self) -> None:
        """投放哨兵并等待线程把队列里剩余的音频播完，再关流。"""
        self._queue.put(_END)
        self._thread.join()
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass


class SingTool:
    """Seeduplex 唱歌工具。

    文本需求 -> 写进系统提示词 -> 起拍语音(trigger.wav) -> 模型演唱 -> 扬声器播放 -> 返回摘要。
    """

    def __init__(self, config_path: str = DEFAULT_CONFIG_PATH):
        self._cfg = _load_cfg(config_path)
        self._ws: Optional[Any] = None
        self._event_id = 0

    # ---- 内部：WebSocket ----
    def _new_event_id(self) -> str:
        self._event_id += 1
        return f"event_{self._event_id}"

    async def _connect(self) -> None:
        headers = {
            "X-Api-App-Key": self._cfg["app_key"],
            "X-Api-App-ID": self._cfg["app_id"],
            "X-Api-Access-Key": self._cfg["api_key"],
            "X-Api-Resource-Id": self._cfg["resource_id"],
            "X-Api-Connect-Id": uuid.uuid4().hex,
        }
        self._ws = await websockets.connect(ENDPOINT, additional_headers=headers, ping_interval=None)

    async def _send(self, event: Dict[str, Any]) -> None:
        await self._ws.send(json.dumps(event, ensure_ascii=False, separators=(",", ":")))

    async def _recv(self) -> Dict[str, Any]:
        return json.loads(await self._ws.recv())

    async def _close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    # ---- 内部：会话 ----
    def _sing_session_payload(self, requirement: str, event_id: str,
                              voice: str, enable_music: bool) -> Dict[str, Any]:
        return {
            "type": "session.create",
            "event_id": event_id,
            "session": {
                "id": uuid.uuid4().hex,
                "model": MODEL,
                "instructions": (
                    "你是一位专业歌手，请以专业水准演唱用户点的歌。\n"
                    "你会唱的歌，就完整演唱；不会唱的，先直接说明「不会唱这首歌」，"
                    "再推荐并演唱一首你会唱的歌曲。\n"
                    f"现在请唱：{requirement}。"
                ),
                "audio": {
                    "input": {"format": {"type": "pcm", "rate": INPUT_RATE}},
                    "output": {"format": {"type": "pcm_s16le", "rate": OUTPUT_RATE}, "voice": voice},
                },
            },
            "extension": {
                "extra": {"enable_proactive_speak": True},
                "dialog": {"extra": {"enable_music": enable_music}},
            },
        }

    def _tts_session_payload(self, event_id: str, voice: str) -> Dict[str, Any]:
        return {
            "type": "session.create",
            "event_id": event_id,
            "session": {
                "id": uuid.uuid4().hex,
                "model": MODEL,
                "audio": {
                    "input": {"format": {"type": "pcm", "rate": INPUT_RATE}},
                    "output": {"format": {"type": "pcm_s16le", "rate": OUTPUT_RATE}, "voice": voice},
                },
            },
        }

    def _new_stream_player(self) -> "_StreamPlayer":
        """按 config 选定输出设备并打印，返回一个边收边播的播放器。"""
        player = _StreamPlayer(
            OUTPUT_RATE,
            device_index=self._cfg["speaker_index"],
            device_name=self._cfg["speaker_name"],
        )
        player.start()
        return player

    # ---- 生成起拍语音 ----
    async def _tts(self, phrase: str) -> bytes:
        """用 Seeduplex 打招呼通道把短语合成语音（pcm_s16le 24k），返回音频字节。"""
        await self._connect()
        await self._send(self._tts_session_payload(self._new_event_id(), self._cfg["voice"]))
        while (await self._recv()).get("type") != "session.created":
            pass
        await self._send({
            "type": "speech_text_buffer.commit",
            "event_id": self._new_event_id(),
            "speech_id": uuid.uuid4().hex,
            "text": phrase,
        })
        buf = bytearray()
        try:
            while True:
                ev = await self._recv()
                et = ev.get("type")
                if et == "response.output_audio.delta":
                    buf += _decode_audio(ev.get("delta"))
                elif et in ("response.done", "response.output_audio.done", "error", "session.closed"):
                    break
        finally:
            await self._close()
        return bytes(buf)

    # ---- 核心流程 ----
    async def _sing_async(self, requirement: str, voice: Optional[str] = None,
                          enable_music: Optional[bool] = None, play: bool = True,
                          trigger_file: str = TRIGGER_FILE) -> Dict[str, Any]:
        voice = voice or self._cfg["voice"]
        if enable_music is None:
            enable_music = self._cfg["enable_music"]

        trigger_audio = _load_trigger_wav(trigger_file)

        # 唱歌会话：需求进提示词 + 起拍语音喂回触发
        await self._connect()
        await self._send(self._sing_session_payload(requirement, self._new_event_id(), voice, enable_music))
        while (await self._recv()).get("type") != "session.created":
            pass
        # 起拍语音整段一次性推送（不按实时语速分片，省掉人为的 ~1s 延迟）
        await self._send({
            "type": "input_audio_buffer.append",
            "audio": base64.b64encode(trigger_audio).decode("ascii"),
        })
        await self._send({"type": "input_audio_buffer.commit", "event_id": self._new_event_id()})
        await self._send({"type": "input_audio_mute.commit", "event_id": self._new_event_id()})

        # 流式播放：先按 config 选定扬声器并拉起播放线程，边收演唱边写进播放器
        player = self._new_stream_player() if play else None

        text = ""
        total_audio_bytes = 0
        usage = None
        try:
            while True:
                ev = await self._recv()
                et = ev.get("type")
                if et == "response.output_text.delta":
                    text += ev.get("delta") or ""
                elif et == "response.output_text.done":
                    text = ev.get("text") or text
                elif et == "response.output_audio.delta":
                    chunk = _decode_audio(ev.get("delta"))
                    if chunk:
                        total_audio_bytes += len(chunk)
                        if player is not None:
                            player.write(chunk)
                elif et == "response.done":
                    usage = (ev.get("response") or {}).get("usage")
                    break
                elif et == "response.output_audio.done":
                    try:
                        ev = await asyncio.wait_for(self._recv(), timeout=2.0)
                        if ev.get("type") == "response.done":
                            usage = (ev.get("response") or {}).get("usage")
                    except (asyncio.TimeoutError, Exception):
                        pass
                    break
                elif et in ("error", "session.closed"):
                    break
        finally:
            await self._close()
            if player is not None:
                player.close()  # 等队列里剩余的音频播完再返回

        if not total_audio_bytes:
            raise RuntimeError("模型未演唱（无音频返回）")

        text = (text or "").strip()
        return {
            "ok": True,
            "text": text,
            "summary": text or "已演唱",
            "voice": voice,
            "duration_ms": int(total_audio_bytes / 2 / OUTPUT_RATE * 1000),
            "audio_bytes": total_audio_bytes,
            "usage": usage,
        }

    # ---- 对外接口 ----
    def sing(self, requirement: str, voice: Optional[str] = None,
             enable_music: Optional[bool] = None) -> str:
        """唱歌并返回摘要字符串（agent 友好）。"""
        return self.sing_detailed(requirement, voice=voice, enable_music=enable_music)["summary"]

    def sing_detailed(self, requirement: str, voice: Optional[str] = None,
                      enable_music: Optional[bool] = None, play: bool = True,
                      trigger_file: str = TRIGGER_FILE) -> Dict[str, Any]:
        """唱歌并返回完整结果（歌词、时长、音色、用量）。"""
        if not (requirement or "").strip():
            raise ValueError("唱歌需求描述不能为空")
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(
                self._sing_async(requirement, voice=voice, enable_music=enable_music,
                                 play=play, trigger_file=trigger_file)
            )
        finally:
            loop.close()


def make_trigger(phrase: str = TRIGGER_PHRASE, out_path: str = TRIGGER_FILE,
                 config_path: str = DEFAULT_CONFIG_PATH) -> str:
    """用 Seeduplex 把起拍短语合成语音，保存为 16k 单声道 wav，返回保存路径。"""
    tool = SingTool(config_path)
    loop = asyncio.new_event_loop()
    try:
        audio_24k = loop.run_until_complete(tool._tts(phrase))
    finally:
        loop.close()
    audio_16k = _resample_s16le(audio_24k, OUTPUT_RATE, INPUT_RATE)
    with wave.open(out_path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(INPUT_RATE)
        w.writeframes(audio_16k)
    return out_path


def sing(requirement: str, config_path: str = DEFAULT_CONFIG_PATH) -> str:
    """快捷函数：新建工具实例并唱歌，返回摘要。"""
    return SingTool(config_path).sing(requirement)


def sing_detailed(requirement: str, config_path: str = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    """快捷函数：新建工具实例并唱歌，返回完整结果。"""
    return SingTool(config_path).sing_detailed(requirement)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sing",
        description="豆包实时语音模型 3.0（Seeduplex）唱歌工具：输入需求描述，现场唱歌并播放，返回歌词摘要。",
    )
    parser.add_argument("requirement", nargs="*",
                        help="唱歌需求描述（可含空格，如英文歌名 My Heart Will Go On），省略时用默认曲目")
    parser.add_argument("-v", "--voice", default=None,
                        help="音色 ID，覆盖 config.toml（如 zh_female_vv_jupiter_bigtts）")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG_PATH,
                        help="config.toml 路径（默认与 sing_tool.py 同目录）")
    parser.add_argument("--no-music", action="store_true", help="关闭唱歌模式（默认开）")
    parser.add_argument("--no-play", action="store_true", help="只返回摘要，不播放（干跑）")
    parser.add_argument("--trigger-file", default=TRIGGER_FILE, help="起拍语音 wav 文件路径（运行时读取）")
    parser.add_argument("--make-trigger", action="store_true", help="合成起拍语音保存到 --trigger-file 后退出")
    parser.add_argument("--trigger-phrase", default=TRIGGER_PHRASE, help="起拍语音文本（配合 --make-trigger）")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return parser


def main(argv: Optional[list] = None) -> None:
    args = _build_parser().parse_args(argv)
    if args.make_trigger:
        out = make_trigger(args.trigger_phrase, args.trigger_file, args.config)
        print(f"起拍语音已生成 -> {out}", file=sys.stderr)
        return
    requirement = " ".join(args.requirement).strip() or DEFAULT_REQUIREMENT
    tool = SingTool(args.config)
    result = tool.sing_detailed(
        requirement,
        voice=args.voice,
        enable_music=False if args.no_music else None,
        play=not args.no_play,
        trigger_file=args.trigger_file,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()