from __future__ import annotations

import logging
import queue
import wave
from collections import deque
from pathlib import Path
from typing import Any

from ..exceptions import AudioCaptureError, MicrophonePermissionError

logger = logging.getLogger(__name__)


class StreamingMicrophone:
    """Raw 16 kHz PCM capture with a bounded callback queue and in-memory ring."""

    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        channels: int = 1,
        chunk_ms: int = 100,
        queue_max_chunks: int = 50,
        ring_buffer_seconds: int = 30,
        stream_factory: Any | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.chunk_ms = chunk_ms
        self.frames_per_chunk = sample_rate * chunk_ms // 1000
        self._queue: queue.Queue[bytes] = queue.Queue(queue_max_chunks)
        ring_chunks = max(1, ring_buffer_seconds * 1000 // chunk_ms)
        self._ring: deque[bytes] = deque(maxlen=ring_chunks)
        self._stream_factory = stream_factory
        self._stream: Any | None = None
        self.dropped_chunks = 0
        self.peak_queue_depth = 0
        self._pressure_warned = False

    @property
    def is_running(self) -> bool:
        return self._stream is not None

    def start(self) -> None:
        if self._stream is not None:
            raise AudioCaptureError("流式麦克风已经启动。")
        try:
            if self._stream_factory is None:
                import sounddevice as sd

                device = sd.query_devices(kind="input")
                if not device or int(device.get("max_input_channels", 0)) < 1:
                    raise AudioCaptureError("未检测到可用的麦克风输入设备。")
                factory = sd.RawInputStream
            else:
                factory = self._stream_factory
            stream = factory(
                samplerate=self.sample_rate,
                blocksize=self.frames_per_chunk,
                channels=self.channels,
                dtype="int16",
                callback=self._audio_callback,
            )
            stream.start()
            self._stream = stream
            logger.info(
                "Streaming microphone started sample_rate=%d chunk_ms=%d queue_max=%d",
                self.sample_rate,
                self.chunk_ms,
                self._queue.maxsize,
            )
        except AudioCaptureError:
            raise
        except Exception as exc:
            message = str(exc).lower()
            if any(word in message for word in ("permission", "denied", "not permitted")):
                raise MicrophonePermissionError(
                    "没有麦克风权限。请在系统设置的隐私与安全性中允许本应用访问。"
                ) from exc
            raise AudioCaptureError(f"无法启动流式麦克风：{exc}") from exc

    def read(self, timeout: float = 0.5) -> bytes | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                raise AudioCaptureError(f"停止流式麦克风失败：{exc}") from exc

    def ring_pcm(self) -> bytes:
        return b"".join(self._ring)

    def clear_pending(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def write_ring_wav(self, destination: Path) -> Path:
        pcm = self.ring_pcm()
        if not pcm:
            raise AudioCaptureError("没有可用于 HTTP 回退的音频。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(self.channels)
            output.setsampwidth(2)
            output.setframerate(self.sample_rate)
            output.writeframes(pcm)
        return destination

    def _audio_callback(
        self, indata: bytes, _frames: int, _time_info: object, status: object
    ) -> None:
        chunk = bytes(indata)
        if status:
            logger.warning("Streaming capture status=%s", status)
        self._ring.append(chunk)
        try:
            self._queue.put_nowait(chunk)
            depth = self._queue.qsize()
            self.peak_queue_depth = max(self.peak_queue_depth, depth)
            if not self._pressure_warned and depth / self._queue.maxsize >= 0.8:
                self._pressure_warned = True
                logger.warning(
                    "Streaming capture queue pressure depth=%d max=%d",
                    depth,
                    self._queue.maxsize,
                )
        except queue.Full:
            self.dropped_chunks += 1
