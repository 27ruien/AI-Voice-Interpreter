from __future__ import annotations

import math
import shutil
import struct
import tempfile
import time
import wave
from collections.abc import Mapping
from pathlib import Path

from ..models import ASRResult, TranslationResult, TTSResult


class MockSpeechRecognizer:
    def __init__(self, text: str = "大家好，感谢参加今天的会议。") -> None:
        self.text = text

    def transcribe(self, audio_path: Path) -> ASRResult:
        if not audio_path.is_file():
            raise FileNotFoundError(f"录音文件不存在：{audio_path}")
        started = time.perf_counter()
        return ASRResult(
            text=self.text,
            language="zh",
            duration_ms=_elapsed_ms(started),
            provider="mock",
            model="mock-asr",
            request_id="mock-asr-request",
        )


class MockTranslator:
    def __init__(
        self,
        translated_text: str = "Hello everyone, thank you for joining today's meeting.",
    ) -> None:
        self.translated_text = translated_text

    def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
        context: Mapping[str, object] | None = None,
    ) -> TranslationResult:
        del context
        started = time.perf_counter()
        return TranslationResult(
            source_text=text,
            translated_text=self.translated_text,
            source_language=source_language,
            target_language=target_language,
            duration_ms=_elapsed_ms(started),
            provider="mock",
            model="mock-translation",
            request_id="mock-translation-request",
        )


class MockTextToSpeech:
    """Creates a valid WAV tone. It deliberately does not pretend to be AI speech."""

    def __init__(self, output_dir: Path | None = None) -> None:
        self._owned_dir = output_dir is None
        self.output_dir = output_dir or Path(tempfile.mkdtemp(prefix="aivi-mock-tts-"))
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def synthesize(self, text: str, voice: str | None = None) -> TTSResult:
        del text
        started = time.perf_counter()
        path = self.output_dir / f"mock-tts-{time.time_ns()}.wav"
        _write_tone(path)
        return TTSResult(
            audio_path=path,
            audio_format="wav",
            duration_ms=_elapsed_ms(started),
            provider="mock",
            model="mock-tts",
            voice=voice or "mock-tone",
            request_id="mock-tts-request",
        )

    def cleanup(self) -> None:
        if self._owned_dir:
            shutil.rmtree(self.output_dir, ignore_errors=True)


def _write_tone(path: Path, duration_seconds: float = 0.35, sample_rate: int = 16000) -> None:
    frame_count = int(duration_seconds * sample_rate)
    amplitude = 2500
    with wave.open(str(path), "wb") as wav_file:
        wav_file.setnchannels(1)
        wav_file.setsampwidth(2)
        wav_file.setframerate(sample_rate)
        frames = (
            struct.pack("<h", int(amplitude * math.sin(2 * math.pi * 440 * i / sample_rate)))
            for i in range(frame_count)
        )
        wav_file.writeframes(b"".join(frames))


def _elapsed_ms(started: float) -> float:
    return max(0.0, (time.perf_counter() - started) * 1000)
