from __future__ import annotations

import wave
from pathlib import Path
from typing import Any

import pytest

from ai_voice_interpreter.config import AppConfig
from ai_voice_interpreter.exceptions import GatewayError
from ai_voice_interpreter.models import PipelineResult
from ai_voice_interpreter.remote.streaming_gateway_client import StreamPacket
from ai_voice_interpreter.streaming.controller import StreamingSessionController


class FakeMicrophone:
    def __init__(self) -> None:
        self.is_running = False
        self.dropped_chunks = 0
        self.clear_calls = 0
        self.peak_queue_depth = 0

    def start(self) -> None:
        self.is_running = True

    def stop(self) -> None:
        self.is_running = False

    def read(self, timeout: float) -> bytes | None:
        del timeout
        return None

    def clear_pending(self) -> None:
        self.clear_calls += 1

    def write_ring_wav(self, destination: Path) -> Path:
        with wave.open(str(destination), "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(16000)
            output.writeframes(b"\1\0" * 1600)
        return destination


class FakeStreamPlayer:
    def __init__(self) -> None:
        self.first_audio_received_at = 0.0
        self.first_playback_at = 0.0
        self.started = False
        self.cleaned = False

    def start_turn(self, **_kwargs: object) -> None:
        self.started = True

    def feed(self, _audio: bytes) -> None:
        self.first_audio_received_at = 1.0
        self.first_playback_at = 1.1

    def stop_turn(self) -> None:
        return None

    def cleanup(self) -> None:
        self.cleaned = True


class FakeClient:
    request_id = "request-test"

    def __init__(self, packets: list[StreamPacket | Exception]) -> None:
        self._packets = packets
        self.closed = False

    def open(self, **_kwargs: object) -> dict[str, str]:
        return {"type": "session.started", "session_id": "session-test"}

    def send_audio(self, _audio: bytes) -> None:
        return None

    def send_ping(self) -> None:
        return None

    def stop_session(self) -> None:
        return None

    def packets(self, timeout: float):
        del timeout
        for packet in self._packets:
            if isinstance(packet, Exception):
                raise packet
            yield packet

    def close(self) -> None:
        self.closed = True


class FakeFallbackPipeline:
    def __init__(self, output: Path) -> None:
        self.output = output
        self.calls = 0

    def process(self, _path: Path) -> PipelineResult:
        self.calls += 1
        return PipelineResult(
            recognized_text="你好",
            translated_text="Hello",
            generated_audio_path=self.output,
        )


class FakeFallbackPlayer:
    def __init__(self) -> None:
        self.played: Path | None = None

    def play(self, path: Path) -> None:
        self.played = path


def config() -> AppConfig:
    return AppConfig(
        app_mode="real",
        interpreter_mode="remote_stream",
        ai_gateway_base_url="https://example.test/gateway",
        ai_gateway_token="test-token",
        network_timeout_seconds=1,
    )


def test_http_fallback_runs_when_stream_fails_before_playback(tmp_path: Path) -> None:
    output = tmp_path / "fallback.wav"
    output.write_bytes(b"RIFFmock")
    pipeline = FakeFallbackPipeline(output)
    fallback_player = FakeFallbackPlayer()
    events: list[dict[str, Any]] = []
    controller = StreamingSessionController(
        config(),
        microphone=FakeMicrophone(),  # type: ignore[arg-type]
        stream_player=FakeStreamPlayer(),  # type: ignore[arg-type]
        client=FakeClient([GatewayError("disconnect")]),  # type: ignore[arg-type]
        fallback_pipeline=pipeline,
        fallback_player=fallback_player,  # type: ignore[arg-type]
    )
    controller.run(on_event=events.append)
    assert pipeline.calls == 1
    assert fallback_player.played == output
    assert any(event.get("fallback") == "http" for event in events)


def test_no_full_sentence_fallback_after_streamed_audio(tmp_path: Path) -> None:
    output = tmp_path / "fallback.wav"
    output.write_bytes(b"RIFFmock")
    pipeline = FakeFallbackPipeline(output)
    controller = StreamingSessionController(
        config(),
        microphone=FakeMicrophone(),  # type: ignore[arg-type]
        stream_player=FakeStreamPlayer(),  # type: ignore[arg-type]
        client=FakeClient(
            [
                StreamPacket(
                    event={
                        "type": "tts.audio.start",
                        "turn_id": "turn-test",
                        "sample_rate": 24000,
                        "channels": 1,
                        "sample_width": 2,
                    }
                ),
                StreamPacket(audio=b"\0\0"),
                GatewayError("disconnect"),
            ]
        ),  # type: ignore[arg-type]
        fallback_pipeline=pipeline,
        fallback_player=FakeFallbackPlayer(),  # type: ignore[arg-type]
    )
    with pytest.raises(GatewayError, match="disconnect"):
        controller.run()
    assert pipeline.calls == 0


def test_websocket_send_queue_full_is_explicit() -> None:
    microphone = FakeMicrophone()
    microphone.read = lambda timeout: b"\0\0"  # type: ignore[method-assign]  # noqa: ARG005
    limited = AppConfig(
        app_mode="real",
        interpreter_mode="remote_stream",
        ai_gateway_base_url="https://example.test/gateway",
        ai_gateway_token="test-token",
        stream_send_queue_max_chunks=1,
    )
    controller = StreamingSessionController(
        limited,
        microphone=microphone,  # type: ignore[arg-type]
        stream_player=FakeStreamPlayer(),  # type: ignore[arg-type]
        client=FakeClient([]),  # type: ignore[arg-type]
    )
    controller._send_queue.put_nowait(b"full")  # noqa: SLF001
    with pytest.raises(GatewayError, match="发送队列已满"):
        controller._capture_pump_loop()  # noqa: SLF001
