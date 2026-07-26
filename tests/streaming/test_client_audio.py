from __future__ import annotations

import json
import ssl
import wave
from pathlib import Path
from typing import Any

import pytest

from ai_voice_interpreter.remote.streaming_gateway_client import StreamingGatewayClient
from ai_voice_interpreter.streaming.microphone import StreamingMicrophone
from ai_voice_interpreter.streaming.player import PCMStreamingPlayer


class FakeConnection:
    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self.responses: list[str | bytes] = [
            json.dumps(
                {
                    "type": "session.started",
                    "session_id": "session-test",
                    "protocol_version": "1.0",
                }
            ),
            b"\0\0" * 100,
            json.dumps({"type": "session.completed"}),
        ]
        self.closed = False

    def send(self, payload: str | bytes) -> None:
        self.sent.append(payload)

    def recv(self, timeout: float | None = None) -> str | bytes:
        del timeout
        return self.responses.pop(0)

    def close(self) -> None:
        self.closed = True


def test_streaming_client_uses_header_and_parses_binary() -> None:
    connection = FakeConnection()
    captured: dict[str, Any] = {}

    def connect_factory(url: str, **kwargs: Any) -> FakeConnection:
        captured.update(url=url, **kwargs)
        return connection

    client = StreamingGatewayClient(
        "https://example.test/tool/gateway", "local-token", connect_factory=connect_factory
    )
    assert client.websocket_url == "wss://example.test/tool/gateway/v1/stream"
    assert client.open()["type"] == "session.started"
    assert captured["additional_headers"] == {"Authorization": "Bearer local-token"}
    assert isinstance(captured["ssl"], ssl.SSLContext)
    assert captured["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert "local-token" not in captured["url"]
    start = json.loads(connection.sent[0])
    assert start["audio"] == {
        "format": "pcm_s16le",
        "sample_rate": 16000,
        "channels": 1,
        "chunk_ms": 100,
    }
    client.send_audio(b"\0\0")
    assert client.receive().audio == b"\0\0" * 100
    assert client.receive().event == {"type": "session.completed"}
    client.close()
    assert connection.closed


def test_microphone_queue_is_bounded_and_ring_writes_wav(tmp_path: Path) -> None:
    microphone = StreamingMicrophone(
        queue_max_chunks=1, ring_buffer_seconds=1, chunk_ms=100
    )
    chunk = b"\x01\0" * 1600
    microphone._audio_callback(chunk, 1600, object(), None)
    microphone._audio_callback(chunk, 1600, object(), None)
    assert microphone.dropped_chunks == 1
    assert microphone.read() == chunk
    path = microphone.write_ring_wav(tmp_path / "ring.wav")
    with wave.open(str(path), "rb") as recording:
        assert recording.getframerate() == 16000
        assert recording.getnchannels() == 1
        assert recording.getsampwidth() == 2
        assert recording.getnframes() == 3200


class FakeOutput:
    def __init__(self, sink: list[bytes], **_kwargs: object) -> None:
        self.sink = sink

    def __enter__(self) -> FakeOutput:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def write(self, pcm: bytes) -> None:
        self.sink.append(pcm)


def test_pcm_player_uses_bounded_worker_and_saves_valid_wav(tmp_path: Path) -> None:
    del tmp_path
    sink: list[bytes] = []
    player = PCMStreamingPlayer(
        prebuffer_ms=20,
        save_last_turn=True,
        output_factory=lambda **kwargs: FakeOutput(sink, **kwargs),
    )
    player.start_turn(sample_rate=24000, channels=1, sample_width=2)
    first = b"\x01\0" * 480
    second = b"\x02\0" * 480
    player.feed(first)
    player.feed(second)
    path = player.stop_turn()
    assert path is not None and path.is_file()
    assert b"".join(sink) == first + second
    assert player.first_playback_at >= player.first_audio_received_at
    with wave.open(str(path), "rb") as audio:
        assert audio.getframerate() == 24000
        assert audio.readframes(960) == first + second
    player.cleanup()
    assert not path.exists()


def test_pcm_player_rejects_audio_before_start() -> None:
    player = PCMStreamingPlayer(output_factory=lambda **_kwargs: object())
    with pytest.raises(Exception, match="tts.audio.start"):
        player.feed(b"\0\0")
    player.cleanup()


def test_pcm_player_stop_timeout_covers_buffered_audio() -> None:
    player = PCMStreamingPlayer(queue_max_seconds=10)
    player.sample_rate = 24000
    player.channels = 1
    player.sample_width = 2
    player._pcm.extend(b"\0\0" * (24000 * 7))  # noqa: SLF001
    assert player._stop_timeout_seconds() == 9.0  # noqa: SLF001
    player.cleanup()
