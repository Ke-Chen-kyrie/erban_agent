#!/usr/bin/env python3
"""
sherpa-onnx 流式 ASR — zipformer bilingual zh-en 本地识别

继承 BaseASR，与 ByteDanceASR 接口完全一致，可通过 ASR_PROVIDER 配置切换。
"""

import os
import time
import queue
import threading
import numpy as np

from asr.base_asr import BaseASR, SentenceResult
from config import SHERPA_ONNX_MODEL_DIR
from logging_config import get_logger

logger = get_logger(__name__)

TARGET_RATE = 16000


class SherpaOnnxASR(BaseASR):
    """sherpa-onnx zipformer 流式 ASR，中英双语，继承 BaseASR"""

    def __init__(self, sample_rate=16000):
        super().__init__(sample_rate=sample_rate)
        self._recognizer = None
        self._loaded = False
        self._model_dir = SHERPA_ONNX_MODEL_DIR
        self._needs_reset = False

    def resume(self):
        super().resume()
        self._needs_reset = True

    def _ensure_loaded(self):
        if self._loaded:
            return
        self.preload()

    def preload(self):
        if self._loaded:
            return

        try:
            import sherpa_onnx
        except ImportError:
            raise ImportError(
                "sherpa-onnx 未安装，请运行: pip install sherpa-onnx>=1.13\n"
                "或切换 ASR_PROVIDER=cloud"
            )

        model_dir = self._model_dir
        if not model_dir or not os.path.isdir(model_dir):
            from modelscope import snapshot_download
            logger.info("[SherpaOnnx] 下载模型: pengzhendong/sherpa-onnx-streaming-zipformer-bilingual-zh-en")
            model_dir = snapshot_download("pengzhendong/sherpa-onnx-streaming-zipformer-bilingual-zh-en")
            self._model_dir = model_dir

        logger.info(f"[SherpaOnnx] 加载模型: {model_dir}")

        self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            encoder=os.path.join(model_dir, "encoder-epoch-99-avg-1.onnx"),
            decoder=os.path.join(model_dir, "decoder-epoch-99-avg-1.onnx"),
            joiner=os.path.join(model_dir, "joiner-epoch-99-avg-1.onnx"),
            tokens=os.path.join(model_dir, "tokens.txt"),
            bpe_vocab=os.path.join(model_dir, "bpe.vocab"),
            num_threads=4,
            model_type="",
            sample_rate=TARGET_RATE,
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.4,
            rule2_min_trailing_silence=0.5,
            rule3_min_utterance_length=180,
        )

        self._loaded = True
        logger.info("[SherpaOnnx] 模型加载完成")

    def start_listening(self, audio_queue):
        if not self._active.is_set():
            return

        self._ensure_loaded()

        my_session = self._session_id

        stream = self._recognizer.create_stream()
        speech_started = False
        speech_begin_wall = 0.0
        last_text = ""

        logger.info("[SherpaOnnx] 开始监听...")
        self.last_speech_time = time.time()

        try:
            while self._active.is_set() and self._session_id == my_session:
                if self._paused.is_set():
                    time.sleep(0.2)
                    continue

                if self._needs_reset:
                    self._needs_reset = False
                    self._recognizer.reset(stream)
                    last_text = ""
                    speech_started = False
                    # 原子 drain：reset 后立即清空队列，避免残留回声污染 stream
                    drained = 0
                    while True:
                        try:
                            audio_queue.get_nowait()
                            drained += 1
                        except queue.Empty:
                            break
                    if drained:
                        logger.info(f"[SherpaOnnx] 流状态已重置（pause→resume），丢弃残留音频 {drained} 帧")
                    else:
                        logger.info("[SherpaOnnx] 流状态已重置（pause→resume）")

                try:
                    audio_chunk = audio_queue.get(timeout=0.2)
                except queue.Empty:
                    continue

                with self._audio_lock:
                    self._audio_buffer.append(audio_chunk.copy())

                stream.accept_waveform(TARGET_RATE, audio_chunk.astype(np.float32))

                while self._recognizer.is_ready(stream):
                    self._recognizer.decode_stream(stream)

                current_text = self._recognizer.get_result(stream)

                if current_text and current_text != last_text:
                    if not speech_started:
                        speech_started = True
                        speech_begin_wall = time.time()
                        self.last_speech_time = time.time()
                        logger.info("[SherpaOnnx] 开始说话")
                        if self.speech_begin_callback:
                            self.speech_begin_callback()
                    last_text = current_text
                    self.last_speech_time = time.time()
                    print(f"\r[SherpaOnnx]: {current_text}", end="", flush=True)

                if speech_started and self._recognizer.is_endpoint(stream):
                    final_text = current_text.strip()
                    if final_text:
                        now = time.time()
                        video_frames = None
                        wav_bytes = self.save_audio_to_wav()
                        logger.info(f"\n[SherpaOnnx 识别完成] {final_text}")
                        print()
                        self.sentence_queue.put(SentenceResult(
                            sentence=final_text, timestamp=now,
                            video_frames=video_frames, speech_begin_time=speech_begin_wall,
                            wav_bytes=wav_bytes,
                        ))

                    self._recognizer.reset(stream)
                    last_text = ""
                    speech_started = False

        except Exception as e:
            logger.error(f"[SherpaOnnx] 错误: {e}")
        finally:
            if self._session_id == my_session:
                self._active.clear()
                self._audio_reserved.clear()
                logger.info("[SherpaOnnx] 识别结束")