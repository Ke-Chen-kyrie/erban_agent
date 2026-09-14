import io
import numpy as np
import queue
import threading
import time
import os
import wave

from sherpa_onnx import KeywordSpotter

from config import (
    WAKEUP_KEYWORD,
    WAKEUP_MODEL_DIR,
    WAKEUP_COOLDOWN_SECONDS,
    WAKEUP_BUFFER_DURATION,
)
from logging_config import get_logger

logger = get_logger(__name__)


class WakeupDetector:
    def __init__(self, sample_rate=16000, audio_capture=None):
        self.sample_rate = sample_rate
        self.wakeup_model = None
        self.is_running = False
        self.wakeup_callback = None
        self.audio_capture = audio_capture
        self._stream = None

        # 冷却机制
        self.last_detection_time = 0
        self.cooldown_seconds = WAKEUP_COOLDOWN_SECONDS

        # 滚动音频缓冲，唤醒时保存用于声纹识别
        self._audio_buffer: list[np.ndarray] = []
        self._buffer_duration = WAKEUP_BUFFER_DURATION
        self._buffer_max_samples = int(sample_rate * self._buffer_duration)

    def load_model(self):
        """加载 sherpa-onnx 唤醒模型（含预热）。"""
        logger.info("正在加载 sherpa-onnx 唤醒模型...")

        model_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            WAKEUP_MODEL_DIR
        )

        # 优先 CUDA，不可用时回退 CPU
        try:
            import torch
            provider = 'cuda' if torch.cuda.is_available() else 'cpu'
        except ImportError:
            provider = 'cpu'
        logger.info(f"  provider={provider}, model_dir={model_dir}")

        keywords_path = self._generate_keywords_file()

        self.wakeup_model = KeywordSpotter(
            tokens=os.path.join(model_dir, "tokens.txt"),
            encoder=os.path.join(model_dir, "encoder-epoch-13-avg-2-chunk-8-left-64.onnx"),
            decoder=os.path.join(model_dir, "decoder-epoch-13-avg-2-chunk-8-left-64.onnx"),
            joiner=os.path.join(model_dir, "joiner-epoch-13-avg-2-chunk-8-left-64.onnx"),
            keywords_file=keywords_path,
            num_threads=4,
            sample_rate=self.sample_rate,
            feature_dim=80,
            max_active_paths=4,
            keywords_score=1.5,
            keywords_threshold=0.25,
            num_trailing_blanks=2,
            provider=provider,
        )

        # 预热推理
        logger.info("预热推理...")
        stream = self.wakeup_model.create_stream()
        silent = np.zeros(int(self.sample_rate * 0.5), dtype=np.float32)
        stream.accept_waveform(self.sample_rate, silent)
        while self.wakeup_model.is_ready(stream):
            self.wakeup_model.decode_stream(stream)
        self.wakeup_model.get_result(stream)
        logger.info("sherpa-onnx 唤醒模型加载完成（已预热）")

    def _generate_keywords_file(self):
        """根据 WAKEUP_KEYWORD 生成 sherpa-onnx 格式的 keywords 文件。"""
        from pypinyin import pinyin, Style

        keyword = WAKEUP_KEYWORD
        pinyins = pinyin(keyword, style=Style.TONE, neutral_tone_with_five=True)

        initials_list = ['zh', 'ch', 'sh', 'b', 'p', 'm', 'f', 'd', 't', 'n', 'l',
                         'g', 'k', 'h', 'j', 'q', 'x', 'r', 'z', 'c', 's', 'y', 'w']

        tokens = []
        for p in pinyins:
            raw = p[0]
            for init in initials_list:
                if raw.startswith(init):
                    tokens.append(init)
                    tokens.append(raw[len(init):])
                    break
            else:
                tokens.append(raw)

        token_str = ' '.join(tokens)
        keywords_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "custom_keywords.txt"
        )
        with open(keywords_path, 'w', encoding='utf-8') as f:
            f.write(f"{token_str} @{keyword}\n")

        logger.info(f"关键词 tokens: {token_str} @{keyword}")
        return keywords_path

    def start(self, audio_queue, callback):
        """启动检测线程。"""
        self.wakeup_callback = callback
        self.is_running = True
        self._stream = self.wakeup_model.create_stream()

        self._worker_thread = threading.Thread(
            target=self._worker_loop, args=(audio_queue,), daemon=True
        )
        self._worker_thread.start()
        logger.info("sherpa-onnx 唤醒检测已启动")

    def stop(self):
        self.is_running = False
        if hasattr(self, '_worker_thread'):
            self._worker_thread.join(timeout=2)
        self._stream = None

    def _save_buffer_to_wav(self) -> bytes:
        """将滚动缓冲导出为 WAV bytes（纯内存）。"""
        if not self._audio_buffer:
            return b""
        audio_data = np.concatenate(self._audio_buffer)
        duration = len(audio_data) / self.sample_rate
        logger.info(f"[wakeup] 缓冲音频: {len(audio_data)} 样本, {duration:.2f}s")
        int_data = (audio_data * 32767).astype(np.int16)

        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.sample_rate)
            wf.writeframes(int_data.tobytes())
        wav_bytes = buf.getvalue()
        logger.info(f"[wakeup] WAV bytes: {len(wav_bytes)} bytes, {duration:.2f}s")
        return wav_bytes

    def _worker_loop(self, audio_queue):
        while self.is_running:
            try:
                # ASR 激活时让出主队列，把音频留给 ASR
                if self.wakeup_callback and self.wakeup_callback.is_asr_active():
                    time.sleep(0.1)
                    continue

                # AI 讲话时让出音频给打断检测
                if self.wakeup_callback and hasattr(self.wakeup_callback, 'is_speaking') and self.wakeup_callback.is_speaking():
                    time.sleep(0.1)
                    continue

                chunk = audio_queue.get(timeout=1)

                # 拿到帧后再检查一次，避免抢走 ASR 或打断检测的音频
                if self.wakeup_callback and self.wakeup_callback.is_asr_active():
                    audio_queue.task_done()
                    continue
                if self.wakeup_callback and hasattr(self.wakeup_callback, 'is_speaking') and self.wakeup_callback.is_speaking():
                    audio_queue.task_done()
                    continue

                # 滚动缓冲（唤醒时用于声纹识别）
                self._audio_buffer.append(chunk)
                total_samples = sum(len(c) for c in self._audio_buffer)
                while total_samples > self._buffer_max_samples and self._audio_buffer:
                    removed = self._audio_buffer.pop(0)
                    total_samples -= len(removed)

                # 流式喂入 sherpa-onnx
                self._stream.accept_waveform(self.sample_rate, chunk)

                # 持续解码直到流中没有足够帧
                while self.wakeup_model.is_ready(self._stream):
                    self.wakeup_model.decode_stream(self._stream)

                # 检查是否检测到关键词
                result = self.wakeup_model.get_result(self._stream)
                audio_queue.task_done()

                if not result:
                    continue

                current_time = time.time()
                if current_time - self.last_detection_time < self.cooldown_seconds:
                    continue

                logger.info(f"\n唤醒成功！检测结果: {result}")
                self.last_detection_time = current_time

                # 保存唤醒音频用于声纹识别
                wav_bytes = self._save_buffer_to_wav()
                self._audio_buffer.clear()

                # 重置流，清空内部缓冲（避免重复误唤醒）
                self.wakeup_model.reset_stream(self._stream)

                # AI 正在讲话时忽略误唤醒，避免打断 TTS 播放
                if self.wakeup_callback and hasattr(self.wakeup_callback, 'is_speaking') and self.wakeup_callback.is_speaking():
                    logger.warning("[唤醒检测] 检测到关键词但 AI 正在讲话，忽略")
                    continue

                # 清空队列中的残留音频，避免 ASR 收到唤醒词前后的旧数据
                while True:
                    try:
                        audio_queue.get_nowait()
                        audio_queue.task_done()
                    except queue.Empty:
                        break

                # 通知主循环
                if self.wakeup_callback:
                    self.wakeup_callback.on_wakeup(wav_bytes)

            except queue.Empty:
                continue
            except Exception as e:
                if self.is_running:
                    logger.error(f"[唤醒检测] 异常: {e}")
                continue