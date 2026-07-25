from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class ASRStreamEvent:
    text: str = ""
    is_sentence_end: bool = False
    request_id: str | None = None
    completed: bool = False


@dataclass(frozen=True, slots=True)
class TranslationStreamEvent:
    text: str
    delta: str
    request_id: str | None = None
    final: bool = False


@dataclass(frozen=True, slots=True)
class TTSAudioStreamEvent:
    audio: bytes = b""
    request_id: str | None = None
    completed: bool = False


class RealtimeASRSession(Protocol):
    async def start(self) -> None: ...
    async def send_audio(self, pcm: bytes) -> None: ...
    async def finish(self) -> None: ...
    async def cancel(self) -> None: ...
    def events(self) -> AsyncIterator[ASRStreamEvent]: ...


class StreamingTranslator(Protocol):
    def translate_stream(
        self, text: str, source_language: str, target_language: str
    ) -> AsyncIterator[TranslationStreamEvent]: ...


class StreamingTTSSession(Protocol):
    sample_rate: int
    channels: int
    sample_width: int

    async def start(self) -> None: ...
    async def send_text(self, text: str) -> None: ...
    async def complete(self) -> None: ...
    async def cancel(self) -> None: ...
    def audio_events(self) -> AsyncIterator[TTSAudioStreamEvent]: ...
