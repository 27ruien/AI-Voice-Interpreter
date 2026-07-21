from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

from ..models import ASRResult, TranslationResult, TTSResult


class SpeechRecognizer(Protocol):
    def transcribe(self, audio_path: Path) -> ASRResult: ...


class Translator(Protocol):
    def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
        context: Mapping[str, object] | None = None,
    ) -> TranslationResult: ...


class TextToSpeech(Protocol):
    def synthesize(self, text: str, voice: str | None = None) -> TTSResult: ...

