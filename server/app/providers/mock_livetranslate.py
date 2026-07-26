from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from typing import Any

from .livetranslate import (
    LiveTranslateProviderError,
    LiveTranslateSessionOptions,
    PCMOutputSpec,
)


class MockLiveTranslateUpstreamSession:
    """Deterministic, no-network LiveTranslate protocol simulator for tests."""

    def __init__(
        self,
        _config: object,
        options: LiveTranslateSessionOptions,
        *,
        scenario: str = "normal",
    ) -> None:
        self.options = options
        self.scenario = scenario
        self.session_id = "mock-live-session"
        self.model = "mock-qwen3.5-livetranslate"
        self.output_spec = PCMOutputSpec(24000)
        self.audio_queue_peak = 0
        self.output_queue_peak = 0
        self.sent_audio: list[bytes] = []
        self.cancelled = False
        self._finish = asyncio.Event()

    async def start(self) -> None:
        if self.scenario == "access_denied_startup":
            raise LiveTranslateProviderError("AccessDenied", "mock access denied")

    async def send_audio(self, pcm: bytes) -> None:
        if not pcm:
            raise LiveTranslateProviderError("EMPTY_AUDIO", "mock empty audio")
        self.sent_audio.append(pcm)
        self.audio_queue_peak = max(self.audio_queue_peak, len(self.sent_audio))

    async def finish(self) -> None:
        self._finish.set()

    async def cancel(self) -> None:
        self.cancelled = True
        self._finish.set()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        await self._finish.wait()
        if self.scenario == "finish_timeout":
            await asyncio.Event().wait()
        if self.scenario == "upstream_disconnect":
            raise LiveTranslateProviderError(
                "UPSTREAM_DISCONNECTED", "mock upstream disconnected"
            )
        if self.scenario in {"access_denied", "unpurchased", "quota_exhausted"}:
            codes = {
                "access_denied": "AccessDenied",
                "unpurchased": "AccessDenied.Unpurchased",
                "quota_exhausted": "QuotaExhausted",
            }
            yield _error(codes[self.scenario], f"mock {self.scenario}")
            return
        if self.scenario == "voice_clone_failed":
            yield _error("voice_clone_failed", "mock voice clone failed")
            return
        events = _normal_events()
        if self.scenario == "source_unavailable":
            events.insert(
                2,
                {
                    "type": "error",
                    "event_id": "mock-source-error",
                    "error": {
                        "code": "PermissionDenied",
                        "message": "source transcription unavailable",
                        "param": "session.input_audio_transcription",
                    },
                },
            )
        if self.scenario == "audio_invalid":
            next(event for event in events if event["type"] == "response.audio.delta")[
                "delta"
            ] = "invalid!"
        if self.scenario == "text_revision":
            index = next(
                index
                for index, event in enumerate(events)
                if event["type"] == "response.audio_transcript.text"
            )
            events.insert(
                index + 1,
                {
                    **events[index],
                    "event_id": "mock-translation-revision",
                    "text": "Hello, today we discuss",
                    "stash": " the delivery plan.",
                },
            )
        if self.scenario == "multi_turn":
            second = _normal_events(suffix="-2")
            events = events[:-1] + second
        for event in events:
            self.output_queue_peak = max(self.output_queue_peak, 1)
            yield event


def _normal_events(suffix: str = "") -> list[dict[str, Any]]:
    response_id = f"mock-response{suffix}"
    source_id = f"mock-source{suffix}"
    item_id = f"mock-output{suffix}"
    audio = base64.b64encode(b"\x00\x00" * 480).decode()
    return [
        {
            "type": "session.created",
            "event_id": f"mock-session-created{suffix}",
            "session": {"id": "mock-live-session"},
        },
        {
            "type": "session.updated",
            "event_id": f"mock-session-updated{suffix}",
            "session": {"id": "mock-live-session", "output_audio_format": "pcm24"},
        },
        {
            "type": "conversation.item.input_audio_transcription.text",
            "event_id": f"mock-source-partial{suffix}",
            "item_id": source_id,
            "text": "你好，",
            "stash": "我们讨论项目进度。",
        },
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "event_id": f"mock-source-final{suffix}",
            "item_id": source_id,
            "transcript": "你好，我们讨论项目进度。",
        },
        {
            "type": "response.created",
            "event_id": f"mock-response-created{suffix}",
            "response": {"id": response_id, "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "event_id": f"mock-output-added{suffix}",
            "response_id": response_id,
            "item": {"id": item_id},
        },
        {
            "type": "response.content_part.added",
            "event_id": f"mock-part-added{suffix}",
            "response_id": response_id,
            "item_id": item_id,
            "part": {"type": "audio"},
        },
        {
            "type": "response.audio_transcript.text",
            "event_id": f"mock-translation-partial{suffix}",
            "response_id": response_id,
            "item_id": item_id,
            "text": "Hello, today we discuss",
            "stash": " project progress.",
        },
        {
            "type": "response.audio.delta",
            "event_id": f"mock-audio{suffix}",
            "response_id": response_id,
            "item_id": item_id,
            "delta": audio,
        },
        {
            "type": "response.audio_transcript.done",
            "event_id": f"mock-translation-done{suffix}",
            "response_id": response_id,
            "item_id": item_id,
            "transcript": "Hello, today we discuss project progress.",
        },
        {
            "type": "response.audio.done",
            "event_id": f"mock-audio-done{suffix}",
            "response_id": response_id,
            "item_id": item_id,
        },
        {
            "type": "response.content_part.done",
            "event_id": f"mock-part-done{suffix}",
            "response_id": response_id,
            "item_id": item_id,
        },
        {
            "type": "response.output_item.done",
            "event_id": f"mock-output-done{suffix}",
            "response_id": response_id,
            "item": {"id": item_id},
        },
        {
            "type": "response.done",
            "event_id": f"mock-response-done{suffix}",
            "response": {
                "id": response_id,
                "status": "completed",
                "usage": {"total_tokens": 10},
            },
        },
        {"type": "session.finished", "event_id": f"mock-session-finished{suffix}"},
    ]


def _error(code: str, message: str) -> dict[str, Any]:
    return {
        "type": "error",
        "event_id": "mock-error",
        "error": {"code": code, "message": message},
    }
