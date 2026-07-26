from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import numpy as np

from .audio_format import StreamingInputAdapter, StreamingOutputAdapter
from .devices import AudioDeviceInfo


@dataclass(slots=True)
class AudioIOMetrics:
    input_queue_peak: int = 0
    output_queue_peak: int = 0
    input_backpressure_count: int = 0
    output_backpressure_count: int = 0
    underrun_count: int = 0
    input_frames: int = 0
    output_frames: int = 0
    rms: float = 0.0
    peak: float = 0.0
    first_output_write_at: float = 0.0


class DirectionalAudioIO:
    """One isolated capture/output route with callback-only bounded queues."""

    def __init__(
        self,
        input_device: AudioDeviceInfo,
        output_device: AudioDeviceInfo,
        *,
        queue_max_chunks: int = 100,
        input_stream_factory: Callable[..., Any] | None = None,
        output_stream_factory: Callable[..., Any] | None = None,
    ) -> None:
        if input_device.max_input_channels <= 0 or output_device.max_output_channels <= 0:
            raise ValueError("方向音频端点缺少输入或输出声道。")
        self.input_device = input_device
        self.output_device = output_device
        self.input_channels = min(2, input_device.max_input_channels)
        self.output_channels = min(2, output_device.max_output_channels)
        self._input_queue: queue.Queue[np.ndarray] = queue.Queue(queue_max_chunks)
        self._output_queue: queue.Queue[np.ndarray] = queue.Queue(queue_max_chunks)
        self._input_adapter = StreamingInputAdapter(
            input_device.default_sample_rate, self.input_channels
        )
        self._output_adapter = StreamingOutputAdapter(
            output_device.default_sample_rate, self.output_channels
        )
        self._input_stream_factory = input_stream_factory
        self._output_stream_factory = output_stream_factory
        self._input_stream: Any | None = None
        self._output_stream: Any | None = None
        self._output_pending = np.empty((0, self.output_channels), dtype=np.float32)
        self._closed = True
        self._lock = threading.Lock()
        self.metrics = AudioIOMetrics()

    @property
    def active(self) -> bool:
        return not self._closed

    def start(self) -> None:
        if not self._closed:
            raise RuntimeError("音频方向已经启动。")
        if self._input_stream_factory is None or self._output_stream_factory is None:
            import sounddevice as sd

            input_factory = self._input_stream_factory or sd.InputStream
            output_factory = self._output_stream_factory or sd.OutputStream
        else:
            input_factory = self._input_stream_factory
            output_factory = self._output_stream_factory
        self._closed = False
        try:
            self._output_stream = output_factory(
                device=self.output_device.index,
                samplerate=self.output_device.default_sample_rate,
                channels=self.output_channels,
                dtype="float32",
                callback=self._output_callback,
                blocksize=0,
            )
            self._input_stream = input_factory(
                device=self.input_device.index,
                samplerate=self.input_device.default_sample_rate,
                channels=self.input_channels,
                dtype="float32",
                callback=self._input_callback,
                blocksize=0,
            )
            self._output_stream.start()
            self._input_stream.start()
        except Exception:
            self.close()
            raise

    def read_input_pcm(self, timeout: float = 0.25) -> bytes:
        try:
            chunk = self._input_queue.get(timeout=timeout)
        except queue.Empty:
            return b""
        return self._input_adapter.process(chunk)

    def enqueue_output_pcm(self, pcm: bytes, *, input_rate: int = 24000) -> None:
        if input_rate != int(self._output_adapter.input_rate):
            if self.metrics.output_frames:
                raise ValueError("输出采样率不能在同一 Turn 中切换。")
            self._output_adapter = StreamingOutputAdapter(
                self.output_device.default_sample_rate,
                self.output_channels,
                input_rate=input_rate,
            )
        converted = self._output_adapter.process(pcm)
        if not len(converted):
            return
        try:
            self._output_queue.put_nowait(converted)
        except queue.Full as exc:
            self.metrics.output_backpressure_count += 1
            raise RuntimeError("方向播放队列已满。") from exc
        self.metrics.output_queue_peak = max(
            self.metrics.output_queue_peak, self._output_queue.qsize()
        )

    def close(self) -> None:
        with self._lock:
            streams = (self._input_stream, self._output_stream)
            self._input_stream = None
            self._output_stream = None
            self._closed = True
        for stream in streams:
            if stream is None:
                continue
            for operation in ("stop", "close"):
                with suppress(Exception):
                    getattr(stream, operation)()
        self._drain(self._input_queue)
        self._drain(self._output_queue)
        self._output_pending = np.empty((0, self.output_channels), dtype=np.float32)
        self._input_adapter.reset()
        self._output_adapter.reset()

    def _input_callback(
        self,
        indata: np.ndarray,
        frames: int,
        _time_info: Any,
        _status: Any,
    ) -> None:
        if self._closed:
            return
        chunk = np.asarray(indata, dtype=np.float32).copy()
        try:
            self._input_queue.put_nowait(chunk)
        except queue.Full:
            self.metrics.input_backpressure_count += 1
            return
        self.metrics.input_frames += frames
        self.metrics.input_queue_peak = max(
            self.metrics.input_queue_peak, self._input_queue.qsize()
        )
        if chunk.size:
            self.metrics.rms = float(np.sqrt(np.mean(np.square(chunk))))
            self.metrics.peak = max(self.metrics.peak, float(np.max(np.abs(chunk))))

    def _output_callback(
        self,
        outdata: np.ndarray,
        frames: int,
        _time_info: Any,
        _status: Any,
    ) -> None:
        outdata.fill(0)
        written = 0
        while written < frames:
            if not len(self._output_pending):
                try:
                    self._output_pending = self._output_queue.get_nowait()
                except queue.Empty:
                    self.metrics.underrun_count += 1
                    break
            take = min(frames - written, len(self._output_pending))
            outdata[written : written + take, : self.output_channels] = (
                self._output_pending[:take]
            )
            self._output_pending = self._output_pending[take:]
            written += take
            self.metrics.output_frames += take
            if take and self.metrics.first_output_write_at == 0:
                self.metrics.first_output_write_at = time.monotonic()

    @staticmethod
    def _drain(target: queue.Queue[Any]) -> None:
        while True:
            try:
                target.get_nowait()
            except queue.Empty:
                return
