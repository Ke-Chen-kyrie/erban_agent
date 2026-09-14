"""流式响应模块 —— 从 main.py 提取的 ResponseStreamer。"""

import asyncio
import base64
import time

from audio.playback_manager import AudioPlaybackManager
from common import PerfMetrics
from tts import ByteDanceTTS
from config import (
    AUDIO_SOURCES,
    BYTEDANCE_TTS_API_KEY, BYTEDANCE_TTS_RESOURCE_ID,
    BYTEDANCE_TTS_SPEAKER, BYTEDANCE_TTS_SAMPLE_RATE,
    BYTEDANCE_TTS_SPEECH_RATE, BYTEDANCE_TTS_URL,
)
from agent.utils import (
    set_tts_fallback, get_tts_fallback,
    is_sleep_requested, clear_sleep_request,
    get_router_timing, get_agent_start_time, get_agent_prep_start_time,
)
from logging_config import get_logger

logger = get_logger(__name__)


class ResponseStreamer:
    """流式处理 Agent 应答：文本打印 + Omni 音频流式播放 + TTS 合成。"""

    def __init__(
        self,
        chat_session,
        audio_pm: AudioPlaybackManager,
        interrupted,
    ):
        self._chat_session = chat_session
        self._audio_pm = audio_pm
        self._interrupted = interrupted
        self._partial_ai_text = ""

    async def stream(
        self,
        audio_base64: str,
        user_text: str = "",
        media: tuple = None,
        perf: PerfMetrics = None,
        send_visual: bool = None,
        gesture_mode: bool = False,
    ) -> tuple[str | None, bool, bool]:
        """流式处理 Agent 应答。

        返回 (partial_text, was_interrupted, sleep_requested)。
        """
        self._interrupted.clear()
        self._partial_ai_text = ""
        sleep_requested = False

        perf = perf or PerfMetrics()
        perf.agent_start_time = time.time()

        # 排空堆积的旧音频
        self._audio_pm.drain_audio_queue()
        audio_started = False

        # TTS 懒初始化状态
        tts_client = None
        tts_consumer_task = None

        # Omni 音频后台消费
        omni_audio_queue: asyncio.Queue = asyncio.Queue()
        omni_consumer_task = None

        async def _omni_consumer():
            nonlocal audio_started
            consumer_chunks = 0
            while True:
                try:
                    pcm = await asyncio.wait_for(omni_audio_queue.get(), timeout=120)
                except asyncio.TimeoutError:
                    break
                if pcm is None:
                    break
                if self._interrupted.is_set():
                    break
                self._audio_pm.ensure_player()
                if not audio_started:
                    audio_started = True
                    perf.omni_first_audio_time = time.time()
                    audio_lat = perf.llm_first_audio_latency
                    if audio_lat is not None:
                        logger.info(f"\nOmni 首音频延时 (首token→首音): {audio_lat:.3f}s")
                    self._audio_pm.start_debug_capture()
                    self._audio_pm.start_interrupt_detection()
                try:
                    self._audio_pm.append_debug_audio(pcm)
                    consumer_chunks += 1
                    await asyncio.get_event_loop().run_in_executor(
                        None, self._audio_pm.write, pcm
                    )
                except Exception as e:
                    logger.error(f"\n[Omni Audio] 播放异常: {e}")
                    break
            logger.debug(f"[Omni consumer] 结束: 写入 {consumer_chunks} 块")

        async def _tts_consumer(client: ByteDanceTTS):
            nonlocal audio_started
            consumer_chunks = 0
            consumer_bytes = 0
            while True:
                try:
                    pcm = await asyncio.wait_for(client.audio_queue.get(), timeout=120)
                    if pcm is None:
                        break
                    if self._interrupted.is_set():
                        break
                    self._audio_pm.ensure_player()
                    if not audio_started:
                        audio_started = True
                        perf.omni_first_audio_time = time.time()
                        audio_lat = perf.llm_first_audio_latency
                        if audio_lat is not None:
                            logger.info(f"\nTTS 首音频延时 (首token→首音): {audio_lat:.3f}s")
                        self._audio_pm.start_interrupt_detection()
                    if self._audio_pm.is_playing():
                        consumer_chunks += 1
                        consumer_bytes += len(pcm)
                        await asyncio.get_event_loop().run_in_executor(
                            None, self._audio_pm.write, pcm
                        )
                    else:
                        logger.warning(f"[TTS consumer] 播放器不存在，丢弃 {len(pcm)} 字节")
                except asyncio.TimeoutError:
                    logger.warning("[TTS] 音频接收超时")
                    break
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"[TTS] 音频播放异常: {e}")
                    break
            logger.debug(
                f"[TTS consumer] 结束: 写入 {consumer_chunks} 块, {consumer_bytes} 字节 "
                f"({(consumer_bytes / (BYTEDANCE_TTS_SAMPLE_RATE * 2) * 1000):.0f}ms @{BYTEDANCE_TTS_SAMPLE_RATE}Hz)"
            )

        async def _start_tts():
            nonlocal tts_client, tts_consumer_task
            if tts_client is not None:
                return
            tts_client = ByteDanceTTS(
                api_key=BYTEDANCE_TTS_API_KEY,
                resource_id=BYTEDANCE_TTS_RESOURCE_ID,
                speaker=BYTEDANCE_TTS_SPEAKER,
                sample_rate=BYTEDANCE_TTS_SAMPLE_RATE,
                speech_rate=BYTEDANCE_TTS_SPEECH_RATE,
                url=BYTEDANCE_TTS_URL,
            )
            try:
                await tts_client.connect()
                await tts_client.start_session()
                set_tts_fallback(False)
                tts_consumer_task = asyncio.create_task(_tts_consumer(tts_client))
                logger.info("[TTS] 会话已创建")
            except Exception as e:
                logger.error(f"[TTS] 初始化失败: {e}，本轮 TTS 跳过（下轮将重试）")
                try:
                    await tts_client.close()
                except Exception:
                    pass
                tts_client = None

        async def _stop_tts(interrupted: bool = False):
            nonlocal tts_client, tts_consumer_task
            if tts_client is None:
                return
            logger.info("[TTS] 会话关闭中...")
            if not interrupted:
                try:
                    await tts_client.finish()
                except Exception as e:
                    logger.warning(f"[TTS] FinishSession 异常: {e}")
            if tts_consumer_task and not tts_consumer_task.done():
                if interrupted:
                    tts_consumer_task.cancel()
                    try:
                        await tts_consumer_task
                    except asyncio.CancelledError:
                        pass
                else:
                    try:
                        await asyncio.wait_for(tts_consumer_task, timeout=120)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        logger.warning("[TTS] 等待音频消费超时")
            try:
                await tts_client.close()
            except Exception as e:
                logger.warning(f"[TTS] 关闭连接异常: {e}")
            tts_client = None
            tts_consumer_task = None

        try:
            print("🤖 AI: ", end="", flush=True)
            stream_ended_early = False
            async for kind, chunk in self._chat_session.chat_stream(
                audio_base64=audio_base64, user_text=user_text, media=media,
                send_visual=send_visual,
                gesture_mode=gesture_mode
            ):
                if self._interrupted.is_set():
                    break
                if is_sleep_requested():
                    logger.info("[sleep] go_sleep 已调用，跳过剩余流事件")
                    break

                if kind == "error":
                    logger.error(f"\n[Agent 流错误] {chunk}")
                    stream_ended_early = True
                    break

                if kind == "audio_reset":
                    logger.warning("[audio] 收到重置信号，丢弃已缓冲音频并重新创建播放器")
                    self._audio_pm.stop_interrupt_detection()
                    if omni_consumer_task and not omni_consumer_task.done():
                        omni_consumer_task.cancel()
                        try:
                            await omni_consumer_task
                        except asyncio.CancelledError:
                            pass
                        omni_consumer_task = None
                    while not omni_audio_queue.empty():
                        try:
                            omni_audio_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    if self._audio_pm.is_playing():
                        await asyncio.get_event_loop().run_in_executor(None, self._audio_pm.close)
                    audio_started = False
                    self._partial_ai_text = ""
                    continue

                if kind == "audio":
                    if omni_consumer_task is None:
                        omni_consumer_task = asyncio.create_task(_omni_consumer())
                    try:
                        pcm_data = base64.b64decode(chunk)
                        await omni_audio_queue.put(pcm_data)
                    except Exception as e:
                        logger.error(f"\n[Omni Audio] 解码失败: {e}")
                    continue

                if kind == "message_end":
                    if AUDIO_SOURCES == "tts" and not get_tts_fallback():
                        await _stop_tts(interrupted=False)
                    if omni_consumer_task and not omni_consumer_task.done():
                        await omni_audio_queue.put(None)
                        try:
                            await asyncio.wait_for(omni_consumer_task, timeout=10)
                        except (asyncio.TimeoutError, asyncio.CancelledError):
                            pass
                        omni_consumer_task = None
                    if self._audio_pm.is_playing():
                        await asyncio.get_event_loop().run_in_executor(None, self._audio_pm.drain)
                        await asyncio.get_event_loop().run_in_executor(None, self._audio_pm.close)
                    self._audio_pm.stop_interrupt_detection()
                    self._audio_pm.drain_audio_queue()
                    audio_started = False
                    perf.agent_first_token_time = None
                    if is_sleep_requested():
                        logger.info("[sleep] go_sleep 工具已调用，停止 Agent 流")
                        break
                    continue

                if kind != "token":
                    continue

                self._partial_ai_text += chunk
                print(chunk, end="", flush=True)

                if AUDIO_SOURCES == "tts" and not get_tts_fallback():
                    await _start_tts()
                    if tts_client:
                        try:
                            await tts_client.send_text(chunk)
                        except Exception as e:
                            logger.warning(f"\n[TTS] 发送文本失败: {e}")

                if perf.agent_first_token_time is None:
                    perf.agent_first_token_time = time.time()

                perf.agent_end_time = time.time()

        except Exception as e:
            logger.error(f"\n[LLM 错误: {e}]")
            stream_ended_early = True
        finally:
            was_interrupted = self._interrupted.is_set()

            if AUDIO_SOURCES == "tts":
                await _stop_tts(interrupted=was_interrupted)

            if omni_consumer_task and not omni_consumer_task.done():
                if was_interrupted:
                    omni_consumer_task.cancel()
                    try:
                        await omni_consumer_task
                    except asyncio.CancelledError:
                        pass
                else:
                    await omni_audio_queue.put(None)
                    try:
                        await asyncio.wait_for(omni_consumer_task, timeout=10)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        pass
                omni_consumer_task = None

            if not was_interrupted:
                was_interrupted = self._interrupted.is_set()

            if stream_ended_early and not was_interrupted:
                logger.warning(f"\n[流异常] Omni 流提前结束，可能因 API 连接断开或服务端超时")

            print()
            if self._audio_pm.is_playing():
                logger.info(
                    f"[audio finally] was_interrupted={was_interrupted} "
                    f"stream_ended_early={stream_ended_early} "
                    f"writes={self._audio_pm._player._write_count}"
                )
                if was_interrupted:
                    logger.warning("[audio finally] 被中断，直接 close（不 drain）")
                    await asyncio.get_event_loop().run_in_executor(None, self._audio_pm.close)
                else:
                    logger.info("[audio finally] 开始 drain...")
                    try:
                        await asyncio.get_event_loop().run_in_executor(None, self._audio_pm.drain)
                    except Exception as e:
                        logger.warning(f"[audio finally] drain 异常: {e}")
                    logger.info("[audio finally] drain 完成，close...")
                    await asyncio.get_event_loop().run_in_executor(None, self._audio_pm.close)

            self._audio_pm.stop_interrupt_detection()
            self._audio_pm.save_debug_audio()
            perf.router_start_time, perf.router_end_time = get_router_timing()
            perf.agent_api_start_time = get_agent_start_time()
            perf.agent_prep_start_time = get_agent_prep_start_time()
            perf.dump()

            sleep_requested = is_sleep_requested()
            if sleep_requested:
                clear_sleep_request()

        partial_text = self._partial_ai_text.strip() if was_interrupted else None
        return partial_text, was_interrupted, sleep_requested
