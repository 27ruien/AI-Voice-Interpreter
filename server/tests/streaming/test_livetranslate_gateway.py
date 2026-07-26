from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from server.app.config import ServerConfig
from server.app.main import create_app
from server.app.providers.livetranslate import (
    LiveTranslateProviderError,
    LiveTranslateSessionOptions,
    PCMOutputSpec,
)
from server.app.providers.mock_streaming import (
    MockRealtimeASRSession,
    MockStreamingTranslator,
    MockStreamingTTSSession,
)
from server.app.streaming.router import StreamingPipelineRouter
from server.app.streaming.session import StreamDependencies
from server.app.streaming.vad import TurnVAD

TOKEN = "livetranslate-test-token"
PCM = b"\x01\x00" * 320


def config(tmp_path: Path, **overrides: object) -> ServerConfig:
    values: dict[str, object] = {
        "dashscope_api_key": "unit-test-api-key",
        "dashscope_workspace_id": "ws-test",
        "dashscope_native_base_url": "https://workspace.example/api/v1",
        "client_test_token": TOKEN,
        "temp_audio_dir": tmp_path / "audio",
        "stream_pipeline_provider": "livetranslate",
        "stream_pipeline_fallback_provider": "modular",
        "livetranslate_session_finish_timeout_seconds": 0.2,
        "vad_min_speech_ms": 60,
        "vad_silence_ms": 60,
        "vad_pre_roll_ms": 20,
    }
    values.update(overrides)
    return ServerConfig(**values)


def modular_dependencies() -> StreamDependencies:
    return StreamDependencies(
        asr_factory=MockRealtimeASRSession,
        translator_factory=MockStreamingTranslator,
        tts_factory=MockStreamingTTSSession,
        vad_factory=lambda: TurnVAD(
            frame_ms=20,
            min_speech_ms=60,
            silence_ms=60,
            pre_roll_ms=20,
            max_turn_ms=1000,
            classifier=lambda frame, _rate: any(frame),
        ),
    )


def start_message(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "type": "session.start",
        "protocol_version": "1.0",
        "request_id": str(uuid.uuid4()),
        "source_language": "zh",
        "target_language": "en",
        "mode": "turn_stream",
        "voice": None,
        "voice_mode": "standard",
        "source_transcription_enabled": True,
        "audio": {
            "format": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
            "chunk_ms": 100,
        },
        "client": {"platform": "macos", "app_version": "test"},
    }
    payload.update(overrides)
    return payload


def normal_events(*, duplicate_done: bool = False) -> list[dict[str, Any]]:
    audio = base64.b64encode(b"\x00\x00" * 480).decode()
    events: list[dict[str, Any]] = [
        {
            "type": "conversation.item.input_audio_transcription.text",
            "event_id": "event-source-partial",
            "item_id": "source-item",
            "text": "你好，",
            "stash": "我们讨论项目进度。",
        },
        {
            "type": "conversation.item.input_audio_transcription.completed",
            "event_id": "event-source-final",
            "item_id": "source-item",
            "transcript": "你好，我们讨论项目进度。",
        },
        {
            "type": "response.created",
            "event_id": "event-response",
            "response": {"id": "response-id", "status": "in_progress"},
        },
        {
            "type": "response.audio_transcript.text",
            "event_id": "event-translation-partial",
            "response_id": "response-id",
            "item_id": "output-item",
            "text": "Hello, we discuss",
            "stash": " project progress.",
        },
        {
            "type": "response.audio.delta",
            "event_id": "event-audio-1",
            "response_id": "response-id",
            "item_id": "output-item",
            "delta": audio,
        },
        {
            "type": "response.audio.delta",
            "event_id": "event-audio-2",
            "response_id": "response-id",
            "item_id": "output-item",
            "delta": audio,
        },
        {
            "type": "response.audio_transcript.done",
            "event_id": "event-translation-final",
            "response_id": "response-id",
            "item_id": "output-item",
            "transcript": "Hello, we discuss project progress.",
        },
        {
            "type": "response.audio.done",
            "event_id": "event-audio-done",
            "response_id": "response-id",
            "item_id": "output-item",
        },
        {
            "type": "response.done",
            "event_id": "event-response-done",
            "response": {
                "id": "response-id",
                "status": "completed",
                "usage": {
                    "total_tokens": 56,
                    "input_tokens": 47,
                    "output_tokens": 9,
                    "input_tokens_details": {"audio_tokens": 27, "text_tokens": 20},
                    "output_tokens_details": {"audio_tokens": 7, "text_tokens": 2},
                },
            },
        },
    ]
    if duplicate_done:
        events.insert(-1, dict(events[6]))
        events.insert(-1, dict(events[7]))
    events.append({"type": "session.finished", "event_id": "event-session-finished"})
    return events


class FakeLiveTranslateUpstream:
    def __init__(
        self,
        _config: ServerConfig,
        options: LiveTranslateSessionOptions,
        *,
        scenario: str = "normal",
    ) -> None:
        self.options = options
        self.scenario = scenario
        self.session_id = "upstream-session-id"
        self.model = "qwen3.5-livetranslate-flash-realtime"
        self.output_spec = PCMOutputSpec(24000)
        self.audio_queue_peak = 0
        self.output_queue_peak = 0
        self.audio_chunks: list[bytes] = []
        self.finished = asyncio.Event()
        self.cancelled = False

    async def start(self) -> None:
        if self.scenario == "startup_failure":
            raise LiveTranslateProviderError("AccessDenied", "provider denied")

    async def send_audio(self, pcm: bytes) -> None:
        self.audio_chunks.append(pcm)
        self.audio_queue_peak = max(self.audio_queue_peak, len(self.audio_chunks))

    async def finish(self) -> None:
        self.finished.set()

    async def cancel(self) -> None:
        self.cancelled = True
        self.finished.set()

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        await self.finished.wait()
        if self.scenario == "finish_timeout":
            await asyncio.Event().wait()
        events = normal_events(duplicate_done=self.scenario == "duplicate_done")
        if self.scenario == "response_first":
            response_created = events.pop(2)
            events.insert(0, response_created)
        if self.scenario == "source_unavailable":
            events.insert(
                0,
                {
                    "type": "error",
                    "event_id": "event-source-error",
                    "error": {
                        "code": "PermissionDenied",
                        "message": "source transcription unavailable",
                        "param": "session.input_audio_transcription",
                    },
                },
            )
        elif self.scenario == "invalid_audio":
            audio_event = next(event for event in events if event["type"] == "response.audio.delta")
            audio_event["delta"] = "invalid base64!"
        elif self.scenario in {"access_denied", "quota"}:
            code = (
                "AccessDenied.Unpurchased"
                if self.scenario == "access_denied"
                else "QuotaExhausted"
            )
            events = [
                {
                    "type": "error",
                    "event_id": "event-provider-error",
                    "error": {"code": code, "message": "controlled provider error"},
                }
            ]
        elif self.scenario == "voice_clone_failed":
            events = [
                {
                    "type": "error",
                    "event_id": "event-clone-error",
                    "error": {
                        "code": "voice_clone_failed",
                        "message": "voice clone failed",
                    },
                }
            ]
        elif self.scenario == "disconnect":
            raise LiveTranslateProviderError("UPSTREAM_DISCONNECTED", "upstream disconnected")
        for event in events:
            self.output_queue_peak = max(self.output_queue_peak, 1)
            yield event


class FakeFactory:
    def __init__(self, scenario: str = "normal") -> None:
        self.scenario = scenario
        self.instances: list[FakeLiveTranslateUpstream] = []

    def __call__(
        self, config: ServerConfig, options: LiveTranslateSessionOptions
    ) -> FakeLiveTranslateUpstream:
        instance = FakeLiveTranslateUpstream(config, options, scenario=self.scenario)
        self.instances.append(instance)
        return instance


def collect_until_terminal(websocket: Any) -> tuple[list[dict[str, Any]], list[bytes]]:
    events: list[dict[str, Any]] = []
    audio: list[bytes] = []
    while True:
        message = websocket.receive()
        if message.get("bytes") is not None:
            audio.append(message["bytes"])
            continue
        if message.get("text") is None:
            return events, audio
        event = json.loads(message["text"])
        events.append(event)
        if event["type"] in {"session.completed", "error"}:
            return events, audio


def test_livetranslate_full_session_maps_text_audio_usage_and_cleanup(
    tmp_path: Path,
) -> None:
    factory = FakeFactory()
    app = create_app(
        config(tmp_path),
        stream_dependencies=modular_dependencies(),
        livetranslate_upstream_factory=factory,
    )
    with TestClient(app).websocket_connect(
        "/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as websocket:
        websocket.send_json(start_message())
        started = websocket.receive_json()
        assert started["type"] == "session.started"
        assert started["pipeline_provider"] == "livetranslate"
        assert started["audio_output"]["sample_rate"] == 24000
        assert websocket.receive_json()["type"] == "provider.started"
        assert websocket.receive_json()["type"] == "voice_clone.status"
        websocket.send_bytes(PCM)
        websocket.send_json({"type": "session.stop", "request_id": str(uuid.uuid4())})
        events, audio = collect_until_terminal(websocket)
    types = [event["type"] for event in events]
    assert types.count("asr.final") == 1
    assert types.count("translation.final") == 1
    assert types.count("tts.audio.start") == 1
    assert types.count("tts.audio.end") == 1
    assert types.count("turn.completed") == 1
    assert types[-1] == "session.completed"
    assert len(audio) == 2
    completed = next(event for event in events if event["type"] == "turn.completed")
    assert completed["translated_text"] == "Hello, we discuss project progress."
    assert completed["upstream_response_id"] == "response-id"
    assert completed["usage"]["total_tokens"] == 56
    assert factory.instances[0].audio_chunks == [PCM]
    assert factory.instances[0].cancelled is True


@pytest.mark.parametrize("scenario", ["duplicate_done", "source_unavailable"])
def test_duplicate_done_and_source_transcription_warning_do_not_break_output(
    tmp_path: Path, scenario: str
) -> None:
    factory = FakeFactory(scenario)
    app = create_app(
        config(tmp_path),
        stream_dependencies=modular_dependencies(),
        livetranslate_upstream_factory=factory,
    )
    with TestClient(app).websocket_connect(
        "/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as websocket:
        websocket.send_json(start_message())
        for _ in range(3):
            websocket.receive_json()
        websocket.send_bytes(PCM)
        websocket.send_json({"type": "session.stop"})
        events, audio = collect_until_terminal(websocket)
    types = [event["type"] for event in events]
    assert types.count("translation.final") == 1
    assert types.count("tts.audio.end") == 1
    assert audio
    if scenario == "source_unavailable":
        assert "source_transcription.unavailable" in types
        assert "warning" in types


def test_response_created_before_source_events_keeps_one_stable_turn_id(
    tmp_path: Path,
) -> None:
    factory = FakeFactory("response_first")
    app = create_app(
        config(tmp_path),
        stream_dependencies=modular_dependencies(),
        livetranslate_upstream_factory=factory,
    )
    with TestClient(app).websocket_connect(
        "/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as websocket:
        websocket.send_json(start_message())
        for _ in range(3):
            websocket.receive_json()
        websocket.send_bytes(PCM)
        websocket.send_json({"type": "session.stop"})
        events, _audio = collect_until_terminal(websocket)
    source_final = next(event for event in events if event["type"] == "asr.final")
    turn_completed = next(event for event in events if event["type"] == "turn.completed")
    assert source_final["turn_id"] == turn_completed["turn_id"]


@pytest.mark.parametrize(
    "scenario,expected_code",
    [
        ("invalid_audio", "INVALID_AUDIO"),
        ("access_denied", "AccessDenied.Unpurchased"),
        ("quota", "QuotaExhausted"),
        ("disconnect", "UPSTREAM_DISCONNECTED"),
        ("voice_clone_failed", "VOICE_CLONE_FAILED"),
        ("finish_timeout", "LIVETRANSLATE_FINISH_TIMEOUT"),
    ],
)
def test_livetranslate_failure_scenarios_are_structured_and_cleanup(
    tmp_path: Path, scenario: str, expected_code: str
) -> None:
    factory = FakeFactory(scenario)
    cfg = config(
        tmp_path,
        livetranslate_session_finish_timeout_seconds=(
            0.01 if scenario == "finish_timeout" else 0.2
        ),
    )
    app = create_app(
        cfg,
        stream_dependencies=modular_dependencies(),
        livetranslate_upstream_factory=factory,
    )
    voice_mode = "clone_once" if scenario == "voice_clone_failed" else "standard"
    with TestClient(app).websocket_connect(
        "/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as websocket:
        websocket.send_json(start_message(voice_mode=voice_mode))
        for _ in range(3):
            websocket.receive_json()
        websocket.send_bytes(PCM)
        websocket.send_json({"type": "session.stop"})
        events, _audio = collect_until_terminal(websocket)
    error = next(event for event in events if event["type"] == "error")
    assert error["code"] == expected_code
    assert "api-key" not in json.dumps(events).lower()
    assert factory.instances[0].cancelled is True
    if scenario == "voice_clone_failed":
        assert any(event["type"] == "voice_clone.status" for event in events)


def test_startup_failure_switches_to_modular_once(tmp_path: Path) -> None:
    factory = FakeFactory("startup_failure")
    app = create_app(
        config(tmp_path),
        stream_dependencies=modular_dependencies(),
        livetranslate_upstream_factory=factory,
    )
    with TestClient(app).websocket_connect(
        "/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as websocket:
        websocket.send_json(start_message())
        started = websocket.receive_json()
        assert started["pipeline_provider"] == "modular"
        assert started["fallback_from"] == "livetranslate"
        assert websocket.receive_json()["type"] == "provider.changed"
        websocket.send_bytes(PCM * 6)
        websocket.send_bytes(b"\x00\x00" * 320 * 3)
        websocket.send_json({"type": "session.stop"})
        events, audio = collect_until_terminal(websocket)
    assert any(event["type"] == "turn.completed" for event in events)
    assert audio
    assert len(factory.instances) == 1


def test_router_rejects_invalid_provider_and_disabled_override(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    router = StreamingPipelineRouter(cfg, modular_dependencies())
    start = start_message(pipeline_provider="modular")
    from ai_voice_interpreter.streaming.protocol import SessionStart

    with pytest.raises(Exception, match="未开放"):
        router.resolve_provider(SessionStart.parse(start))
    invalid = config(tmp_path, stream_pipeline_provider="invalid")
    invalid_router = StreamingPipelineRouter(invalid, modular_dependencies())
    with pytest.raises(Exception, match="必须是"):
        invalid_router.resolve_provider(SessionStart.parse(start_message()))


def test_router_fallback_guard_prevents_second_switch_and_post_audio_switch() -> None:
    assert StreamingPipelineRouter.should_automatically_fallback(
        output_started=False, automatic_switches=0
    )
    assert not StreamingPipelineRouter.should_automatically_fallback(
        output_started=True, automatic_switches=0
    )
    assert not StreamingPipelineRouter.should_automatically_fallback(
        output_started=False, automatic_switches=1
    )


def test_client_disconnect_cancels_upstream_without_background_billing(tmp_path: Path) -> None:
    factory = FakeFactory()
    app = create_app(
        config(tmp_path),
        stream_dependencies=modular_dependencies(),
        livetranslate_upstream_factory=factory,
    )
    with TestClient(app).websocket_connect(
        "/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as websocket:
        websocket.send_json(start_message())
        for _ in range(3):
            websocket.receive_json()
        websocket.send_bytes(PCM)
    assert factory.instances[0].cancelled is True
