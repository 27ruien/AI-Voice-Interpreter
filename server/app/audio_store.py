from __future__ import annotations

import os
import time
import uuid
import wave
from dataclasses import dataclass
from pathlib import Path

from .errors import GatewayHTTPError


@dataclass(frozen=True, slots=True)
class WavMetadata:
    duration_seconds: float
    frames: int


class AudioStore:
    def __init__(self, root: Path, ttl_seconds: int) -> None:
        self.root = root
        self.ttl_seconds = ttl_seconds
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def new_input_path(self) -> Path:
        return self.root / f"input-{uuid.uuid4()}.wav"

    def publish(self, generated_path: Path) -> tuple[str, Path]:
        audio_id = str(uuid.uuid4())
        destination = self.root / f"output-{audio_id}.wav"
        os.replace(generated_path, destination)
        return audio_id, destination

    def resolve(self, audio_id: str) -> Path | None:
        try:
            normalized = str(uuid.UUID(audio_id))
        except ValueError:
            return None
        path = self.root / f"output-{normalized}.wav"
        if not path.is_file():
            return None
        if time.time() - path.stat().st_mtime >= self.ttl_seconds:
            path.unlink(missing_ok=True)
            return None
        return path

    def cleanup_expired(self, now: float | None = None) -> int:
        cutoff = (now if now is not None else time.time()) - self.ttl_seconds
        deleted = 0
        for path in self.root.glob("output-*.wav"):
            if path.is_file() and path.stat().st_mtime <= cutoff:
                path.unlink(missing_ok=True)
                deleted += 1
        return deleted


def validate_wav(path: Path) -> WavMetadata:
    try:
        with path.open("rb") as handle:
            header = handle.read(12)
        if len(header) < 12 or header[:4] != b"RIFF" or header[8:12] != b"WAVE":
            raise GatewayHTTPError(400, "invalid_audio", "文件不是有效的 WAV。")
        with wave.open(str(path), "rb") as wav:
            channels = wav.getnchannels()
            sample_rate = wav.getframerate()
            sample_width = wav.getsampwidth()
            frames = wav.getnframes()
            compressed = wav.getcomptype() != "NONE"
    except GatewayHTTPError:
        raise
    except (EOFError, wave.Error, OSError) as exc:
        raise GatewayHTTPError(400, "invalid_audio", "WAV 文件损坏或无法读取。") from exc
    if compressed or channels != 1 or sample_rate != 16000 or sample_width != 2:
        raise GatewayHTTPError(
            400,
            "unsupported_audio",
            "当前仅支持单声道、16 kHz、16-bit PCM WAV。",
        )
    if frames <= 0:
        raise GatewayHTTPError(400, "empty_audio", "WAV 中没有音频帧。")
    return WavMetadata(duration_seconds=frames / sample_rate, frames=frames)
