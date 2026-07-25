from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.app.config import ServerConfig
from server.app.main import create_app
from server.app.providers.mock_streaming import (
    MockRealtimeASRSession,
    MockStreamingTranslator,
    MockStreamingTTSSession,
)
from server.app.streaming.session import StreamDependencies
from server.app.streaming.vad import TurnVAD

from .protocol import AudioInputSpec, SessionStart, new_id

MOCK_TOKEN = "local-streaming-soak-token"
SPEECH = b"\x01\0" * 320
SILENCE = b"\0\0" * 320


def build_mock_app(temp_dir: Path) -> FastAPI:
    config = ServerConfig(
        dashscope_api_key="mock-only-key",
        dashscope_workspace_id="mock-workspace",
        dashscope_native_base_url="https://mock.invalid/api/v1",
        client_test_token=MOCK_TOKEN,
        temp_audio_dir=temp_dir,
        vad_min_speech_ms=60,
        vad_silence_ms=60,
        vad_pre_roll_ms=20,
    )
    dependencies = StreamDependencies(
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
    return create_app(config, stream_dependencies=dependencies)


def run_mock_turn(client: TestClient) -> dict[str, Any]:
    start = SessionStart(
        request_id=new_id(),
        source_language="zh",
        target_language="en",
        mode="turn_stream",
        voice=None,
        audio=AudioInputSpec("pcm_s16le", 16000, 1, 100),
        client_platform="mock-harness",
        app_version="0.2.0",
    )
    events: list[dict[str, Any]] = []
    audio_bytes = 0
    audio_chunks = 0
    client_started = time.perf_counter()
    first_binary_at = 0.0
    with client.websocket_connect(
        "/v1/stream", headers={"Authorization": f"Bearer {MOCK_TOKEN}"}
    ) as websocket:
        websocket.send_json(start.to_message())
        websocket.receive_json()
        websocket.send_bytes(SPEECH * 6)
        speech_ended_at = time.perf_counter()
        websocket.send_bytes(SILENCE * 3)
        websocket.send_json({"type": "session.stop", "request_id": new_id()})
        while True:
            message = websocket.receive()
            if message.get("bytes") is not None:
                if not first_binary_at:
                    first_binary_at = time.perf_counter()
                audio = message["bytes"]
                audio_bytes += len(audio)
                audio_chunks += 1
                continue
            text = message.get("text")
            if text is None:
                break
            event = json.loads(text)
            events.append(event)
            if event["type"] == "session.completed":
                break
    completed = next(event for event in events if event["type"] == "turn.completed")
    metrics = {key: float(value) for key, value in completed["metrics"].items()}
    metrics["client_first_playback_ms"] = max(
        0.0, (first_binary_at - speech_ended_at) * 1000
    )
    metrics["end_to_end_ttfa_ms"] = max(0.0, (first_binary_at - client_started) * 1000)
    return {
        "success": bool(audio_bytes and audio_chunks),
        "fallback": False,
        "metrics": metrics,
        "audio_bytes": audio_bytes,
        "audio_chunks": audio_chunks,
        "event_count": len(events),
    }
