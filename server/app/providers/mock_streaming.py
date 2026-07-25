from __future__ import annotations

import asyncio

from .streaming_interfaces import ASRStreamEvent, TranslationStreamEvent, TTSAudioStreamEvent


class MockRealtimeASRSession:
    def __init__(self, text: str = "你好，我们讨论项目进度。") -> None:
        self.text = text
        self.queue: asyncio.Queue[ASRStreamEvent] = asyncio.Queue(8)
        self.sent_bytes = 0
        self.cancelled = False
        self._partial_sent = False

    async def start(self) -> None:
        return None

    async def send_audio(self, pcm: bytes) -> None:
        self.sent_bytes += len(pcm)
        if not self._partial_sent and self.sent_bytes >= 3200:
            self._partial_sent = True
            await self.queue.put(
                ASRStreamEvent(text=self.text[:6], request_id="mock-asr-request")
            )

    async def finish(self) -> None:
        await self.queue.put(
            ASRStreamEvent(
                text=self.text,
                is_sentence_end=True,
                request_id="mock-asr-request",
            )
        )
        await self.queue.put(ASRStreamEvent(completed=True, request_id="mock-asr-request"))

    async def cancel(self) -> None:
        self.cancelled = True

    async def events(self):  # type: ignore[no-untyped-def]
        while True:
            event = await self.queue.get()
            yield event
            if event.completed:
                break


class MockStreamingTranslator:
    def __init__(self, text: str = "Hello, today we discuss the project progress.") -> None:
        self.text = text
        self.calls = 0

    async def translate_stream(
        self, _text: str, _source_language: str, _target_language: str
    ):
        self.calls += 1
        current = ""
        pieces = ["Hello, today ", "we discuss the ", "project progress."]
        for piece in pieces:
            current += piece
            yield TranslationStreamEvent(
                text=current,
                delta=piece,
                request_id="mock-translation-request",
            )
        yield TranslationStreamEvent(
            text=self.text,
            delta="",
            request_id="mock-translation-request",
            final=True,
        )


class MockStreamingTTSSession:
    sample_rate = 24000
    channels = 1
    sample_width = 2

    def __init__(self, *, chunk_count: int = 3, queue_size: int = 20) -> None:
        self.queue: asyncio.Queue[TTSAudioStreamEvent] = asyncio.Queue(queue_size)
        self.chunk_count = chunk_count
        self.texts: list[str] = []
        self.cancelled = False

    async def start(self) -> None:
        return None

    async def send_text(self, text: str) -> None:
        self.texts.append(text)
        for _ in range(self.chunk_count):
            await self.queue.put(
                TTSAudioStreamEvent(
                    audio=b"\0\0" * 480,
                    request_id="mock-tts-request",
                )
            )

    async def complete(self) -> None:
        await self.queue.put(
            TTSAudioStreamEvent(completed=True, request_id="mock-tts-request")
        )

    async def cancel(self) -> None:
        self.cancelled = True

    async def audio_events(self):  # type: ignore[no-untyped-def]
        while True:
            event = await self.queue.get()
            yield event
            if event.completed:
                break
