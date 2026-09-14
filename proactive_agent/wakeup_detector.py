import numpy as np
import queue
import threading
import time
import os

from sherpa_onnx import KeywordSpotter

from config import (
    WAKEUP_KEYWORD,
    INTERRUPT_KEYWORD,
    INTERRUPT_MODE,
    WAKEUP_MODEL_DIR,
    WAKEUP_COOLDOWN_SECONDS,
)
from logging_config import get_logger

logger = get_logger(__name__)


class WakeupDetector:
    def __init__(self, sample_rate=16000, audio_capture=None, interrupted_event=None, interrupt_keyword=None):
        self.sample_rate = sample_rate
        self.wakeup_model = None
        self.is_running = False
        self.wakeup_callback = None
        self.audio_capture = audio_capture
        self._stream = None
        self._interrupted_event = interrupted_event
        self._interrupt_keyword = interrupt_keyword or INTERRUPT_KEYWORD

        # 冷却机制
        self.last_detection_time = 0
        self.cooldown_seconds = WAKEUP_COOLDOWN_SECONDS

    def load_model(self):
        """加载 sherpa-onnx 唤醒模型（含预热）。"""
        logger.info("正在加载 sherpa-onnx 唤醒模型...")

        model_dir = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
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
        """根据 WAKEUP_KEYWORD + INTERRUPT_KEYWORD 生成 sherpa-onnx 格式的 keywords 文件。"""
        from pypinyin import pinyin, Style

        keywords = [
            (WAKEUP_KEYWORD, WAKEUP_KEYWORD),
            (self._interrupt_keyword, self._interrupt_keyword),
        ]

        initials_list = ['zh', 'ch', 'sh', 'b', 'p', 'm', 'f', 'd', 't', 'n', 'l',
                         'g', 'k', 'h', 'j', 'q', 'x', 'r', 'z', 'c', 's', 'y', 'w']

        lines = []
        for keyword, label in keywords:
            pinyins = pinyin(keyword, style=Style.TONE, neutral_tone_with_five=True)
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
            lines.append(f"{token_str} @{label}")

        keywords_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "custom_keywords.txt"
        )
        with open(keywords_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines) + '\n')

        logger.info(f"关键词: {lines}")
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

    def _is_speaking(self) -> bool:
        """AI 是否正在讲话（供打断检测/让出判断）。"""
        return (
            self.wakeup_callback is not None
            and hasattr(self.wakeup_callback, 'is_speaking')
            and self.wakeup_callback.is_speaking()
        )

    def _should_yield(self, asr_active: bool, speaking: bool) -> bool:
        """KWS 是否应让出音频队列。

        - keyword 模式：AI 讲话时 KWS 需继续消费听打断词，只在「ASR 激活且未讲话」时让出。
        - vad 模式：维持现状——唤醒后（ASR 激活）或 AI 讲话时都让出，打断靠服务端 VAD。
        """
        if INTERRUPT_MODE == "keyword":
            return asr_active and not speaking
        return asr_active or speaking

    def _worker_loop(self, audio_queue):
        _yielding = False  # 跟踪是否刚从让出状态恢复

        while self.is_running:
            try:
                asr_active = self.wakeup_callback and self.wakeup_callback.is_asr_active()
                speaking = self._is_speaking()

                # 让出主队列，把音频留给 feeder（喂给 Qwen / 服务端 VAD）
                if self._should_yield(asr_active, speaking):
                    if not _yielding:
                        self.wakeup_model.reset_stream(self._stream)
                        _yielding = True
                    time.sleep(0.1)
                    continue

                # 恢复消费时丢弃队列中积压的旧帧，只处理新鲜音频
                if _yielding:
                    while True:
                        try:
                            audio_queue.get_nowait()
                            audio_queue.task_done()
                        except queue.Empty:
                            break
                    _yielding = False

                chunk = audio_queue.get(timeout=1)

                # 拿到帧后再检查一次，避免抢走 feeder 的音频
                asr_active = self.wakeup_callback and self.wakeup_callback.is_asr_active()
                speaking = self._is_speaking()
                if self._should_yield(asr_active, speaking):
                    audio_queue.task_done()
                    continue

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

                logger.info(f"\n检测到关键词: {result}")
                self.last_detection_time = current_time

                # 重置流，清空内部缓冲（避免重复误触发）
                self.wakeup_model.reset_stream(self._stream)

                # 检测到关键词的瞬间重新读说话状态
                speaking = self._is_speaking()

                # keyword 模式：AI 讲话中命中打断词 → 触发打断
                if (
                    speaking
                    and INTERRUPT_MODE == "keyword"
                    and result == self._interrupt_keyword
                ):
                    if self._interrupted_event is not None:
                        logger.info(f"[唤醒检测] AI 讲话中检测到打断词 '{result}'，触发打断")
                        self._interrupted_event.set()
                    # 清空队列残留（打断词尾音），避免 feeder 重开后喂给 Qwen
                    while True:
                        try:
                            audio_queue.get_nowait()
                            audio_queue.task_done()
                        except queue.Empty:
                            break
                    continue

                # AI 正在讲话但命中其他词（唤醒词等）→ 忽略
                if speaking:
                    logger.warning(f"[唤醒检测] AI 讲话中检测到 '{result}'，忽略")
                    continue

                # 未讲话命中打断词 → 忽略
                if result == self._interrupt_keyword:
                    logger.info(f"[唤醒检测] 未讲话时检测到打断词 '{result}'，忽略")
                    continue

                # 未讲话命中唤醒词 → 正常唤醒流程
                # 清空队列中的残留音频，避免 feeder 收到唤醒词前后的旧数据
                while True:
                    try:
                        audio_queue.get_nowait()
                        audio_queue.task_done()
                    except queue.Empty:
                        break

                # 通知主循环
                if self.wakeup_callback:
                    self.wakeup_callback.on_wakeup()

            except queue.Empty:
                continue
            except Exception as e:
                if self.is_running:
                    logger.error(f"[唤醒检测] 异常: {e}")
                continue