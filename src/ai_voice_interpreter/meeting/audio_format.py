from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import soxr


def _to_float32(samples: np.ndarray) -> np.ndarray:
    if np.issubdtype(samples.dtype, np.integer):
        maximum = float(max(abs(np.iinfo(samples.dtype).min), np.iinfo(samples.dtype).max + 1))
        return samples.astype(np.float32) / maximum
    return samples.astype(np.float32, copy=False)


def _float_to_pcm_s16le(samples: np.ndarray) -> bytes:
    clipped = np.clip(samples, -1.0, 1.0 - 1.0 / 32768.0)
    return np.rint(clipped * 32768.0).astype("<i2").tobytes()


@dataclass(slots=True)
class AdapterMetrics:
    input_frames: int = 0
    output_frames: int = 0
    incomplete_bytes: int = 0


class StreamingInputAdapter:
    """Stateful device-rate multichannel input to 16 kHz mono S16LE."""

    def __init__(self, input_rate: float, input_channels: int) -> None:
        if input_rate <= 0 or input_channels <= 0:
            raise ValueError("输入采样率和声道必须大于 0。")
        self.input_rate = float(input_rate)
        self.input_channels = input_channels
        self._resampler = soxr.ResampleStream(
            self.input_rate, 16000.0, 1, dtype="float32", quality="HQ"
        )
        self._byte_tail = b""
        self.metrics = AdapterMetrics()

    def process(self, audio: bytes | np.ndarray) -> bytes:
        samples = self._decode(audio)
        if samples.size == 0:
            return b""
        mono = _to_float32(samples).mean(axis=1, dtype=np.float32)
        output = self._resampler.resample_chunk(mono, last=False)
        self.metrics.input_frames += len(mono)
        self.metrics.output_frames += len(output)
        return _float_to_pcm_s16le(output)

    def finish(self) -> bytes:
        output = self._resampler.resample_chunk(np.empty(0, dtype=np.float32), last=True)
        self.metrics.output_frames += len(output)
        self.metrics.incomplete_bytes += len(self._byte_tail)
        self._byte_tail = b""
        return _float_to_pcm_s16le(output)

    def reset(self) -> None:
        self._resampler.clear()
        self._byte_tail = b""
        self.metrics = AdapterMetrics()

    def _decode(self, audio: bytes | np.ndarray) -> np.ndarray:
        if isinstance(audio, bytes):
            payload = self._byte_tail + audio
            frame_bytes = self.input_channels * 2
            usable = len(payload) - len(payload) % frame_bytes
            self._byte_tail = payload[usable:]
            if not usable:
                return np.empty((0, self.input_channels), dtype=np.int16)
            return np.frombuffer(payload[:usable], dtype="<i2").reshape(
                -1, self.input_channels
            )
        samples = np.asarray(audio)
        if samples.ndim == 1:
            if self.input_channels != 1:
                usable = len(samples) - len(samples) % self.input_channels
                samples = samples[:usable].reshape(-1, self.input_channels)
            else:
                samples = samples.reshape(-1, 1)
        if samples.ndim != 2 or samples.shape[1] < self.input_channels:
            raise ValueError("输入音频数组声道数无效。")
        return samples[:, : self.input_channels]


class StreamingOutputAdapter:
    """Stateful 24 kHz mono S16LE to device-rate float32 channel frames."""

    def __init__(
        self,
        output_rate: float,
        output_channels: int,
        *,
        input_rate: float = 24000.0,
    ) -> None:
        if output_rate <= 0 or output_channels <= 0 or input_rate <= 0:
            raise ValueError("输出采样率和声道必须大于 0。")
        self.input_rate = float(input_rate)
        self.output_rate = float(output_rate)
        self.output_channels = output_channels
        self._resampler = soxr.ResampleStream(
            self.input_rate, self.output_rate, 1, dtype="float32", quality="HQ"
        )
        self._byte_tail = b""
        self.metrics = AdapterMetrics()

    def process(self, pcm: bytes) -> np.ndarray:
        payload = self._byte_tail + pcm
        usable = len(payload) - len(payload) % 2
        self._byte_tail = payload[usable:]
        if not usable:
            return np.empty((0, self.output_channels), dtype=np.float32)
        mono = np.frombuffer(payload[:usable], dtype="<i2").astype(np.float32) / 32768.0
        output = self._resampler.resample_chunk(mono, last=False)
        self.metrics.input_frames += len(mono)
        self.metrics.output_frames += len(output)
        return self._map_channels(output)

    def finish(self) -> np.ndarray:
        output = self._resampler.resample_chunk(np.empty(0, dtype=np.float32), last=True)
        self.metrics.output_frames += len(output)
        self.metrics.incomplete_bytes += len(self._byte_tail)
        self._byte_tail = b""
        return self._map_channels(output)

    def reset(self) -> None:
        self._resampler.clear()
        self._byte_tail = b""
        self.metrics = AdapterMetrics()

    def _map_channels(self, mono: np.ndarray) -> np.ndarray:
        mapped = np.zeros((len(mono), self.output_channels), dtype=np.float32)
        if len(mono):
            mapped[:, 0] = mono
            if self.output_channels >= 2:
                mapped[:, 1] = mono
        return mapped


def can_create_resampler(input_rate: float, output_rate: float, channels: int) -> bool:
    try:
        stream = soxr.ResampleStream(
            float(input_rate), float(output_rate), int(channels), dtype="float32"
        )
        stream.clear()
        return True
    except (TypeError, ValueError, RuntimeError):
        return False


def package_version() -> str:
    import importlib.metadata

    return importlib.metadata.version("soxr")
