from __future__ import annotations

import logging
import queue
import shutil
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..exceptions import PlaybackError

logger = logging.getLogger(__name__)


class PCMStreamingPlayer:
    """Bounded PCM writer that never performs device I/O in a network callback."""

    def __init__(
        self,
        *,
        prebuffer_ms: int = 150,
        queue_max_seconds: int = 10,
        save_last_turn: bool = True,
        output_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.prebuffer_ms = prebuffer_ms
        self.queue_max_seconds = queue_max_seconds
        self.save_last_turn = save_last_turn
        self._output_factory = output_factory
        self._queue: queue.Queue[bytes | None] | None = None
        self._thread: threading.Thread | None = None
        self._error: Exception | None = None
        self._pcm = bytearray()
        self._temp_dir = Path(tempfile.mkdtemp(prefix="aivi-stream-playback-"))
        self.last_turn_path: Path | None = None
        self.first_audio_received_at = 0.0
        self.first_playback_at = 0.0
        self.peak_queue_depth = 0
        self.underruns = 0
        self._pressure_warned = False
        self.sample_rate = 24000
        self.channels = 1
        self.sample_width = 2

    def start_turn(self, *, sample_rate: int, channels: int, sample_width: int) -> None:
        self.stop_turn(discard=True)
        if sample_rate <= 0 or channels != 1 or sample_width != 2:
            raise PlaybackError("流式播放器仅支持单声道 16-bit PCM。")
        self.sample_rate = sample_rate
        self.channels = channels
        self.sample_width = sample_width
        max_chunks = max(4, self.queue_max_seconds * 20)
        self._queue = queue.Queue(max_chunks)
        self._pcm.clear()
        self._error = None
        self.first_audio_received_at = 0.0
        self.first_playback_at = 0.0
        self._thread = threading.Thread(target=self._writer, daemon=True)
        self._thread.start()

    def feed(self, pcm: bytes) -> None:
        if not pcm or len(pcm) % self.sample_width:
            raise PlaybackError("收到无效的 PCM 音频块。")
        if self._queue is None:
            raise PlaybackError("尚未收到 tts.audio.start。")
        if self.first_audio_received_at == 0:
            self.first_audio_received_at = time.monotonic()
        self._pcm.extend(pcm)
        try:
            self._queue.put(pcm, timeout=1)
        except queue.Full as exc:
            raise PlaybackError("播放队列已满，已停止流式会话。") from exc
        self.peak_queue_depth = max(self.peak_queue_depth, self._queue.qsize())
        if not self._pressure_warned and self._queue.qsize() / self._queue.maxsize >= 0.8:
            self._pressure_warned = True
            logger.warning(
                "Streaming playback queue pressure depth=%d max=%d",
                self._queue.qsize(),
                self._queue.maxsize,
            )

    def stop_turn(self, *, discard: bool = False) -> Path | None:
        work_queue, thread = self._queue, self._thread
        self._queue = None
        self._thread = None
        if work_queue is not None:
            try:
                work_queue.put(None, timeout=1)
            except queue.Full:
                self._error = PlaybackError("播放队列停止时仍然满载。")
                try:
                    work_queue.get_nowait()
                    work_queue.put_nowait(None)
                except (queue.Empty, queue.Full):
                    pass
        if thread is not None:
            thread.join(timeout=5)
            if thread.is_alive():
                raise PlaybackError("流式播放线程未能及时结束。")
        if self._error is not None:
            raise PlaybackError(f"流式播放失败：{self._error}") from self._error
        if self._pcm:
            logger.info(
                "Streaming playback turn ended bytes=%d first_playback_delay_ms=%.1f "
                "queue_peak=%d underruns=%d",
                len(self._pcm),
                max(0.0, (self.first_playback_at - self.first_audio_received_at) * 1000),
                self.peak_queue_depth,
                self.underruns,
            )
        if discard or not self._pcm or not self.save_last_turn:
            return None
        path = self._temp_dir / f"last-turn-{time.time_ns()}.wav"
        with wave.open(str(path), "wb") as output:
            output.setnchannels(self.channels)
            output.setsampwidth(self.sample_width)
            output.setframerate(self.sample_rate)
            output.writeframes(self._pcm)
        if self.last_turn_path is not None:
            self.last_turn_path.unlink(missing_ok=True)
        self.last_turn_path = path
        return path

    def cleanup(self) -> None:
        self.stop_turn(discard=True)
        shutil.rmtree(self._temp_dir, ignore_errors=True)
        self.last_turn_path = None

    def _writer(self) -> None:
        assert self._queue is not None
        work_queue = self._queue
        try:
            if self._output_factory is None:
                import sounddevice as sd

                factory = sd.RawOutputStream
            else:
                factory = self._output_factory
            bytes_per_second = self.sample_rate * self.channels * self.sample_width
            threshold = bytes_per_second * self.prebuffer_ms // 1000
            buffered = bytearray()
            with factory(
                samplerate=self.sample_rate,
                channels=self.channels,
                dtype="int16",
            ) as stream:
                while True:
                    try:
                        chunk = work_queue.get(timeout=0.1)
                    except queue.Empty:
                        if self.first_playback_at:
                            self.underruns += 1
                        continue
                    if chunk is None:
                        if buffered:
                            self._write(stream, bytes(buffered))
                        return
                    buffered.extend(chunk)
                    if len(buffered) >= threshold:
                        self._write(stream, bytes(buffered))
                        buffered.clear()
        except Exception as exc:
            self._error = exc

    def _write(self, stream: Any, pcm: bytes) -> None:
        if self.first_playback_at == 0:
            self.first_playback_at = time.monotonic()
        stream.write(pcm)
