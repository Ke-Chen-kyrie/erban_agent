"""性能指标收集器 —— 记录各阶段延时。"""

from typing import Optional
from logging_config import get_logger

logger = get_logger(__name__)


class PerfMetrics:
    """性能指标收集器，记录各阶段延时。"""

    def __init__(self):
        self.speech_begin_time: Optional[float] = None
        self.asr_sentence_time: Optional[float] = None
        self.voiceprint_start: Optional[float] = None
        self.voiceprint_end: Optional[float] = None
        self.video_encode_start: Optional[float] = None
        self.video_encode_end: Optional[float] = None
        self.router_start_time: Optional[float] = None
        self.router_end_time: Optional[float] = None
        self.parallel_done_time: Optional[float] = None
        self.agent_start_time: Optional[float] = None
        self.agent_api_start_time: Optional[float] = None
        self.agent_prep_start_time: Optional[float] = None
        self.agent_first_token_time: Optional[float] = None
        self.agent_end_time: Optional[float] = None
        self.omni_first_audio_time: Optional[float] = None

    @property
    def asr_latency(self) -> Optional[float]:
        if self.speech_begin_time and self.asr_sentence_time:
            return self.asr_sentence_time - self.speech_begin_time
        return None

    @property
    def voiceprint_latency(self) -> Optional[float]:
        if self.voiceprint_start and self.voiceprint_end:
            return self.voiceprint_end - self.voiceprint_start
        return None

    @property
    def video_encode_latency(self) -> Optional[float]:
        if self.video_encode_start and self.video_encode_end:
            return self.video_encode_end - self.video_encode_start
        return None

    @property
    def router_latency(self) -> Optional[float]:
        if self.router_start_time and self.router_end_time:
            return self.router_end_time - self.router_start_time
        return None

    @property
    def agent_schedule_latency(self) -> Optional[float]:
        if self.agent_start_time and self.agent_api_start_time:
            return self.agent_api_start_time - self.agent_start_time
        return None

    @property
    def pre_agent_latency(self) -> Optional[float]:
        if self.parallel_done_time and self.agent_start_time:
            return self.agent_start_time - self.parallel_done_time
        return None

    @property
    def llm_first_token_latency(self) -> Optional[float]:
        if self.agent_api_start_time and self.agent_first_token_time:
            return self.agent_first_token_time - self.agent_api_start_time
        return None

    @property
    def llm_prep_latency(self) -> Optional[float]:
        if self.agent_prep_start_time and self.agent_api_start_time:
            return self.agent_api_start_time - self.agent_prep_start_time
        return None

    @property
    def llm_first_token_total(self) -> Optional[float]:
        if self.agent_prep_start_time and self.agent_first_token_time:
            return self.agent_first_token_time - self.agent_prep_start_time
        return None

    @property
    def llm_first_audio_latency(self) -> Optional[float]:
        if self.agent_first_token_time and self.omni_first_audio_time:
            return self.omni_first_audio_time - self.agent_first_token_time
        return None

    @property
    def llm_total_latency(self) -> Optional[float]:
        if self.agent_api_start_time and self.agent_end_time:
            return self.agent_end_time - self.agent_api_start_time
        return None

    @property
    def end_to_end_latency(self) -> Optional[float]:
        if self.speech_begin_time and self.omni_first_audio_time:
            return self.omni_first_audio_time - self.speech_begin_time
        return None

    def dump(self):
        lines = ["\n--- 性能指标 ---"]
        if self.asr_latency is not None:
            lines.append(f"  ASR 延时 (开始说话→句子就绪): {self.asr_latency:.3f}s")
        if self.voiceprint_latency is not None:
            lines.append(f"  声纹延时: {self.voiceprint_latency:.3f}s")
        if self.video_encode_latency is not None:
            lines.append(f"  视频编码延时: {self.video_encode_latency:.3f}s")
        if self.router_latency is not None:
            lines.append(f"  视觉路由延时: {self.router_latency:.3f}s")
        if self.pre_agent_latency is not None and self.pre_agent_latency > 0.01:
            lines.append(f"  预处理延时 (并行完成→应答开始): {self.pre_agent_latency:.3f}s")
        if self.agent_schedule_latency is not None:
            lines.append(f"  Agent 调度延时 (应答开始→API调用): {self.agent_schedule_latency:.3f}s")
        if self.llm_first_token_latency is not None:
            if self.llm_prep_latency is not None and self.llm_prep_latency > 0.01:
                total = self.llm_first_token_total
                if total:
                    lines.append(f"  LLM 首 token 延时 (准备→首token): {total:.3f}s (准备{self.llm_prep_latency:.3f}s + API{self.llm_first_token_latency:.3f}s)")
                else:
                    lines.append(f"  LLM 首 token 延时 (API调用→首token): {self.llm_first_token_latency:.3f}s")
            else:
                lines.append(f"  LLM 首 token 延时 (API调用→首token): {self.llm_first_token_latency:.3f}s")
        if self.llm_total_latency is not None:
            lines.append(f"  LLM 总耗时 (API调用→生成结束): {self.llm_total_latency:.3f}s")
        if self.llm_first_audio_latency is not None:
            lines.append(f"  首音频延时 (首token→首音): {self.llm_first_audio_latency:.3f}s")
        if self.end_to_end_latency is not None:
            lines.append(f"  端到端总延时 (开始说话→首音): {self.end_to_end_latency:.3f}s")
        lines.append("---------------")
        logger.info("\n".join(lines))