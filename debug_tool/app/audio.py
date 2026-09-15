from __future__ import annotations

import io
import threading
import wave
from typing import Any

import numpy as np
import sounddevice as sd
from PySide6.QtCore import QObject, Signal


class AudioRecorder(QObject):
    level_changed = Signal(float)
    elapsed_changed = Signal(float)
    finished = Signal(bytes, float)
    failed = Signal(str)
    playback_failed = Signal(str)
    state_changed = Signal(str)

    def __init__(self, samplerate: int = 16000) -> None:
        super().__init__()
        self.samplerate = samplerate
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._play_thread: threading.Thread | None = None
        self._level_smooth = 0.0

    @property
    def recording(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def paused(self) -> bool:
        return self.recording and self._pause_event.is_set()

    def start(self, seconds: int) -> None:
        if self.recording:
            return
        self._stop_event.clear()
        self._pause_event.clear()
        self._level_smooth = 0.0
        self.state_changed.emit("录音中")
        self._thread = threading.Thread(target=self._record, args=(seconds,), daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    def wait(self, timeout: float = 2.0) -> None:
        if self._thread is not None:
            self._thread.join(timeout)

    def shutdown(self) -> None:
        self.stop()
        self.wait()
        try:
            sd.stop()
        except Exception:
            pass

    def pause(self) -> None:
        if not self.recording or self._pause_event.is_set():
            return
        self._pause_event.set()
        self._level_smooth = 0.0
        self.level_changed.emit(0.0)
        self.state_changed.emit("录音暂停")

    def resume(self) -> None:
        if not self.recording or not self._pause_event.is_set():
            return
        self._pause_event.clear()
        self.state_changed.emit("录音中")

    def toggle_pause(self) -> bool:
        if self.paused:
            self.resume()
            return False
        self.pause()
        return self.paused

    def play(self, audio: bytes) -> None:
        if self._play_thread is not None and self._play_thread.is_alive():
            return
        self._play_thread = threading.Thread(target=self._play, args=(audio,), daemon=True)
        self._play_thread.start()

    def _record(self, seconds: int) -> None:
        chunks: list[np.ndarray] = []
        recorded_samples = 0
        recorded_lock = threading.Lock()

        def callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
            del frames, time_info, status
            nonlocal recorded_samples
            if self._pause_event.is_set():
                self._level_smooth = 0.0
                self.level_changed.emit(0.0)
                with recorded_lock:
                    elapsed = recorded_samples / self.samplerate
                self.elapsed_changed.emit(elapsed)
                return
            chunks.append(indata.copy())
            with recorded_lock:
                recorded_samples += len(indata)
                elapsed = recorded_samples / self.samplerate
            samples = indata.astype(np.float32).reshape(-1) / 32768.0
            if samples.size:
                rms = float(np.sqrt(np.mean(samples * samples)))
                peak = float(np.max(np.abs(samples)))
                level = min(1.0, max(rms * 85.0, peak * 9.0))
                if level > 0.01:
                    level = max(level, 0.06)
                self._level_smooth = self._level_smooth * 0.62 + level * 0.38
            else:
                self._level_smooth *= 0.75
            self.level_changed.emit(min(1.0, self._level_smooth))
            self.elapsed_changed.emit(elapsed)

        try:
            with sd.InputStream(samplerate=self.samplerate, channels=1, dtype="int16", callback=callback):
                while not self._stop_event.is_set():
                    with recorded_lock:
                        elapsed = recorded_samples / self.samplerate
                    if elapsed >= seconds:
                        break
                    sd.sleep(50)
                    self.elapsed_changed.emit(elapsed)
            if not chunks:
                raise RuntimeError("没有录到音频，请检查麦克风权限或设备")
            pcm = np.concatenate(chunks, axis=0)
            duration = len(pcm) / self.samplerate
            self.finished.emit(_encode_wav(pcm, self.samplerate), duration)
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self._level_smooth = 0.0
            self.level_changed.emit(0.0)
            self._stop_event.clear()
            self._pause_event.clear()
            self.state_changed.emit("麦克风未开启")

    def _play(self, audio: bytes) -> None:
        try:
            with wave.open(io.BytesIO(audio), "rb") as wav_file:
                frames = wav_file.readframes(wav_file.getnframes())
                data = np.frombuffer(frames, dtype=np.int16)
                sd.play(data, wav_file.getframerate())
                sd.wait()
        except Exception as exc:
            self.playback_failed.emit(f"回放失败：{exc}")


def _encode_wav(pcm: np.ndarray, samplerate: int) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(samplerate)
        wav_file.writeframes(pcm.astype("<i2").tobytes())
    return buffer.getvalue()
