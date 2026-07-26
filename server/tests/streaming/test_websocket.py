from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from server.app.config import ServerConfig
from server.app.main import create_app
from server.app.providers.mock_streaming import (
    MockRealtimeASRSession,
    MockStreamingTranslator,
    MockStreamingTTSSession,
)
from server.app.providers.streaming_interfaces import TranslationStreamEvent
from server.app.streaming.session import StreamDependencies
from server.app.streaming.vad import TurnVAD

TOKEN = "stream-unit-test-token"
SPEECH = b"\x01\x00" * 320
SILENCE = b"\0\0" * 320


def config(tmp_path: Path, **overrides: object) -> ServerConfig:
    values: dict[str, object] = {
        "dashscope_api_key": "unit-test-api-key",
        "dashscope_workspace_id": "ws-test",
        "dashscope_native_base_url": "https://workspace.example/api/v1",
        "client_test_token": TOKEN,
        "temp_audio_dir": tmp_path / "audio",
        "vad_min_speech_ms": 60,
        "vad_silence_ms": 60,
        "vad_pre_roll_ms": 20,
        "stream_pipeline_provider": "modular",
    }
    values.update(overrides)
    return ServerConfig(**values)


def dependencies(
    translator_factory: object = MockStreamingTranslator,
) -> StreamDependencies:
    return StreamDependencies(
        asr_factory=MockRealtimeASRSession,
        translator_factory=translator_factory,  # type: ignore[arg-type]
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


def test_real_modular_dependencies_are_direction_aware(tmp_path: Path) -> None:
    selected = StreamDependencies.real(config(tmp_path)).for_direction("en", "zh")
    asr = selected.asr_factory()
    tts = selected.tts_factory()
    assert asr.config.source_language == "en"
    assert tts.config.target_language == "zh"


def start_message(version: str = "1.0") -> dict[str, object]:
    return {
        "type": "session.start",
        "protocol_version": version,
        "request_id": str(uuid.uuid4()),
        "source_language": "zh",
        "target_language": "en",
        "mode": "turn_stream",
        "voice": None,
        "audio": {
            "format": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
            "chunk_ms": 100,
        },
        "client": {"platform": "macos", "app_version": "test"},
    }


def test_websocket_auth_rejects_missing_and_wrong_token(tmp_path: Path) -> None:
    client = TestClient(create_app(config(tmp_path), stream_dependencies=dependencies()))
    for headers in ({}, {"Authorization": "Bearer wrong"}):
        with (
            pytest.raises(WebSocketDisconnect) as error,
            client.websocket_connect("/v1/stream", headers=headers),
        ):
            pass
        assert error.value.code == 4401


def test_binary_before_start_returns_structured_error(tmp_path: Path) -> None:
    client = TestClient(create_app(config(tmp_path), stream_dependencies=dependencies()))
    with client.websocket_connect(
        "/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as websocket:
        websocket.send_bytes(SPEECH)
        event = websocket.receive_json()
        assert event["type"] == "error"
        assert event["code"] == "INVALID_SESSION_START"
        assert event["session_id"]
        assert event["request_id"]


def test_unsupported_protocol_version_is_explicit(tmp_path: Path) -> None:
    client = TestClient(create_app(config(tmp_path), stream_dependencies=dependencies()))
    with client.websocket_connect(
        "/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as websocket:
        websocket.send_json(start_message("2.0"))
        event = websocket.receive_json()
        assert event["code"] == "PROTOCOL_VERSION_UNSUPPORTED"


def test_mock_streaming_full_turn_and_stop_flush(tmp_path: Path) -> None:
    client = TestClient(create_app(config(tmp_path), stream_dependencies=dependencies()))
    with client.websocket_connect(
        "/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as websocket:
        websocket.send_json(start_message())
        assert websocket.receive_json()["type"] == "session.started"
        websocket.send_bytes(SPEECH * 6)
        websocket.send_bytes(SILENCE * 3)
        websocket.send_json({"type": "session.stop", "request_id": str(uuid.uuid4())})
        json_events: list[dict[str, object]] = []
        binary_frames: list[bytes] = []
        while True:
            message = websocket.receive()
            if message.get("bytes") is not None:
                binary_frames.append(message["bytes"])
                continue
            if message.get("text") is None:
                break
            event = json.loads(message["text"])
            json_events.append(event)
            if event["type"] == "session.completed":
                break
        types = [event["type"] for event in json_events]
        assert "asr.partial" in types
        assert types.count("asr.final") == 1
        assert "translation.partial" in types
        assert types.count("translation.final") == 1
        assert "tts.audio.start" in types
        assert "tts.audio.end" in types
        assert "turn.completed" in types
        assert binary_frames
        completed = next(event for event in json_events if event["type"] == "turn.completed")
        assert completed["provider_request_ids"] == {
            "asr": "mock-asr-request",
            "translation": "mock-translation-request",
            "tts": "mock-tts-request",
        }


def test_per_token_connection_limit(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            config(tmp_path, streaming_max_connections_per_token=1),
            stream_dependencies=dependencies(),
        )
    )
    with client.websocket_connect(
        "/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
    ):
        with pytest.raises(WebSocketDisconnect) as error, client.websocket_connect(
            "/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
        ):
            pass
        assert error.value.code == 4429


def test_frame_size_limit_returns_error(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            config(tmp_path, streaming_max_frame_bytes=100),
            stream_dependencies=dependencies(),
        )
    )
    with client.websocket_connect(
        "/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as websocket:
        websocket.send_json(start_message())
        websocket.receive_json()
        websocket.send_bytes(b"x" * 101)
        assert websocket.receive_json()["code"] == "AUDIO_FRAME_TOO_LARGE"


class DelayedTranslator:
    async def translate_stream(
        self, _text: str, _source_language: str, _target_language: str
    ):
        yield TranslationStreamEvent(
            text="Hello waiting",
            delta="Hello waiting",
            request_id="delayed-translation-request",
        )
        await asyncio.sleep(0.1)
        yield TranslationStreamEvent(
            text="Hello waiting",
            delta="",
            request_id="delayed-translation-request",
            final=True,
        )


def test_tts_max_wait_emits_audio_before_delayed_translation_final(tmp_path: Path) -> None:
    client = TestClient(
        create_app(
            config(
                tmp_path,
                tts_text_min_chars=5,
                tts_text_target_chars=10,
                tts_text_max_chars=20,
                tts_text_max_wait_ms=20,
            ),
            stream_dependencies=dependencies(DelayedTranslator),
        )
    )
    with client.websocket_connect(
        "/v1/stream", headers={"Authorization": f"Bearer {TOKEN}"}
    ) as websocket:
        websocket.send_json(start_message())
        websocket.receive_json()
        websocket.send_bytes(SPEECH * 6)
        websocket.send_bytes(SILENCE * 3)
        websocket.send_json({"type": "session.stop", "request_id": str(uuid.uuid4())})
        order: list[str] = []
        while True:
            message = websocket.receive()
            if message.get("bytes") is not None:
                order.append("binary")
                continue
            if message.get("text") is None:
                break
            event = json.loads(message["text"])
            order.append(event["type"])
            if event["type"] == "session.completed":
                break
    assert "translation.final" in order
    assert "binary" in order
    assert order.index("binary") < order.index("translation.final")
