from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import numpy as np
import soundfile as sf

from ..exceptions import AudioCaptureError, MicrophonePermissionError

logger = logging.getLogger(__name__)


class MicrophoneRecorder:
    def __init__(
        self,
        sample_rate: int = 16000,
        channels: int = 1,
        keep_temp_audio: bool = False,
        minimum_duration_seconds: float = 0.35,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.keep_temp_audio = keep_temp_audio
        self.minimum_duration_seconds = minimum_duration_seconds
        self._stream: Any | None = None
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._started_at = 0.0
        self._temp_dir = Path(tempfile.mkdtemp(prefix="aivi-recording-"))
        self.last_audio_path: Path | None = None

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self.is_recording:
            raise AudioCaptureError("录音已在进行中。")
        try:
            import sounddevice as sd

            device = sd.query_devices(kind="input")
            if not device or int(device.get("max_input_channels", 0)) < 1:
                raise AudioCaptureError("未检测到可用的麦克风输入设备。")
            with self._lock:
                self._frames.clear()
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
                callback=self._audio_callback,
            )
            self._stream.start()
            self._started_at = time.perf_counter()
            logger.info(
                "Recording started sample_rate=%s channels=%s",
                self.sample_rate,
                self.channels,
            )
        except AudioCaptureError:
            self._stream = None
            raise
        except Exception as exc:
            self._stream = None
            message = str(exc).lower()
            logger.exception("Recording start failed type=%s", type(exc).__name__)
            if any(token in message for token in ("permission", "not permitted", "denied")):
                raise MicrophonePermissionError(
                    "没有麦克风权限。请到 系统设置 → 隐私与安全性 → 麦克风，"
                    "允许 Terminal 或本应用访问。"
                ) from exc
            raise AudioCaptureError(f"无法启动麦克风：{exc}") from exc

    def stop(self) -> Path:
        stream = self._stream
        if stream is None:
            raise AudioCaptureError("当前没有正在进行的录音。")
        self._stream = None
        try:
            stream.stop()
            stream.close()
        except Exception as exc:
            logger.exception("Recording stream close failed")
            raise AudioCaptureError(f"停止录音失败：{exc}") from exc

        with self._lock:
            frames = list(self._frames)
            self._frames.clear()
        duration_seconds = sum(len(frame) for frame in frames) / self.sample_rate
        logger.info("Recording stopped duration_seconds=%.3f", duration_seconds)
        if duration_seconds < self.minimum_duration_seconds:
            raise AudioCaptureError(
                f"录音太短（{duration_seconds:.2f} 秒），"
                f"请至少说满 {self.minimum_duration_seconds:.2f} 秒。"
            )
        data = np.concatenate(frames, axis=0)
        path = self._temp_dir / f"recording-{time.time_ns()}.wav"
        try:
            sf.write(path, data, self.sample_rate, subtype="PCM_16", format="WAV")
        except Exception as exc:
            logger.exception("Writing recording failed")
            raise AudioCaptureError(f"保存临时录音失败：{exc}") from exc
        self.last_audio_path = path
        return path

    def cancel(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.abort()
                stream.close()
            except Exception:
                logger.exception("Failed to abort recording stream")
        with self._lock:
            self._frames.clear()

    def cleanup(self) -> None:
        self.cancel()
        if not self.keep_temp_audio:
            shutil.rmtree(self._temp_dir, ignore_errors=True)
            self.last_audio_path = None

    def _audio_callback(
        self,
        indata: np.ndarray,
        _frames: int,
        _time: object,
        status: object,
    ) -> None:
        if status:
            logger.warning("Audio callback status=%s", status)
        with self._lock:
            self._frames.append(indata.copy())
