from __future__ import annotations

import asyncio
import os
import time
import wave
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_voice_interpreter.models import PipelineResult
from server.app.audio_store import AudioStore
from server.app.config import ServerConfig
from server.app.errors import GatewayHTTPError
from server.app.main import ConcurrencyGate, create_app

TOKEN = "unit-test-client-token"
API_KEY = "unit-test-api-secret"


def make_wav(path: Path, frame_count: int = 320) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * frame_count)
    return path


def settings(tmp_path: Path, **overrides: object) -> ServerConfig:
    values: dict[str, object] = {
        "dashscope_api_key": API_KEY,
        "dashscope_workspace_id": "ws-unit-test",
        "dashscope_native_base_url": "https://workspace.example/api/v1",
        "client_test_token": TOKEN,
        "temp_audio_dir": tmp_path / "audio",
    }
    values.update(overrides)
    return ServerConfig(**values)


class FakePipeline:
    def __init__(self, output_dir: Path, *, error: str | None = None) -> None:
        self.output_dir = output_dir
        self.error = error
        self.calls = 0

    def process(self, _path: Path) -> PipelineResult:
        self.calls += 1
        if self.error:
            return PipelineResult(error=self.error)
        output = make_wav(self.output_dir / f"generated-{self.calls}.wav")
        return PipelineResult(
            recognized_text="你好",
            translated_text="Hello",
            generated_audio_path=output,
            asr_latency_ms=10,
            translation_latency_ms=20,
            tts_latency_ms=30,
            total_latency_ms=60,
            providers={"asr": "fake", "translation": "fake", "tts": "fake"},
            models={"asr": "asr", "translation": "mt", "tts": "tts"},
            request_ids={"asr": "asr-id", "translation": "mt-id", "tts": "tts-id"},
        )


@pytest.fixture
def gateway(tmp_path: Path) -> tuple[TestClient, FakePipeline, ServerConfig]:
    config = settings(tmp_path)
    pipeline = FakePipeline(config.temp_audio_dir)
    return TestClient(create_app(config, pipeline=pipeline)), pipeline, config


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def upload(client: TestClient, wav: Path, **kwargs: object):
    return client.post(
        "/v1/interpret",
        headers=auth(),
        files={"audio": ("../../untrusted.wav", wav.read_bytes(), "audio/wav")},
        data={"source_language": "zh", "target_language": "en", **kwargs},
    )


def test_health_and_ready_are_non_secret(
    gateway: tuple[TestClient, FakePipeline, ServerConfig],
) -> None:
    client, pipeline, _config = gateway
    health = client.get("/healthz")
    ready = client.get("/readyz")
    assert health.status_code == 200
    assert health.json()["service"] == "ai-voice-interpreter-gateway"
    assert ready.status_code == 200
    assert ready.json()["api_key"] == "configured"
    assert pipeline.calls == 0
    assert API_KEY not in health.text + ready.text
    assert TOKEN not in health.text + ready.text


@pytest.mark.parametrize("headers", [{}, auth("wrong-token")])
def test_interpret_requires_correct_token(
    gateway: tuple[TestClient, FakePipeline, ServerConfig],
    tmp_path: Path,
    headers: dict[str, str],
) -> None:
    client, pipeline, _config = gateway
    wav = make_wav(tmp_path / "input.wav")
    response = client.post(
        "/v1/interpret",
        headers=headers,
        files={"audio": ("input.wav", wav.read_bytes(), "audio/wav")},
    )
    assert response.status_code == 401
    assert response.json()["request_id"]
    assert pipeline.calls == 0


def test_valid_request_ignores_filename_publishes_audio_and_deletes_input(
    gateway: tuple[TestClient, FakePipeline, ServerConfig], tmp_path: Path
) -> None:
    client, pipeline, config = gateway
    response = upload(client, make_wav(tmp_path / "input.wav"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["recognized_text"] == "你好"
    assert body["translated_text"] == "Hello"
    assert body["provider_request_ids"]["tts"] == "tts-id"
    assert body["request_id"]
    assert not list(config.temp_audio_dir.glob("input-*.wav"))
    assert not (tmp_path.parent / "untrusted.wav").exists()
    download = client.get(body["audio_url"], headers=auth())
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("audio/wav")
    assert download.content[:4] == b"RIFF"
    assert pipeline.calls == 1


def test_invalid_wav_and_oversize_are_rejected_before_pipeline(
    gateway: tuple[TestClient, FakePipeline, ServerConfig], tmp_path: Path
) -> None:
    client, pipeline, _config = gateway
    invalid = client.post(
        "/v1/interpret",
        headers=auth(),
        files={"audio": ("fake.wav", b"not a wav", "audio/wav")},
    )
    assert invalid.status_code == 400
    assert invalid.json()["request_id"]
    assert pipeline.calls == 0

    small_config = settings(tmp_path / "small", max_upload_mb=1)
    small_pipeline = FakePipeline(small_config.temp_audio_dir)
    oversized = TestClient(create_app(small_config, pipeline=small_pipeline)).post(
        "/v1/interpret",
        headers=auth(),
        files={"audio": ("large.wav", b"x" * (1024 * 1024 + 1), "audio/wav")},
    )
    assert oversized.status_code == 413
    assert small_pipeline.calls == 0
    assert not list(small_config.temp_audio_dir.glob("input-*.wav"))


def test_provider_failure_has_request_id_and_no_audio(
    tmp_path: Path,
) -> None:
    config = settings(tmp_path)
    client = TestClient(
        create_app(config, pipeline=FakePipeline(config.temp_audio_dir, error="ASR failed"))
    )
    response = upload(client, make_wav(tmp_path / "input.wav"))
    assert response.status_code == 502
    assert response.json()["request_id"]
    assert not list(config.temp_audio_dir.glob("output-*.wav"))
    assert not list(config.temp_audio_dir.glob("input-*.wav"))


def test_audio_requires_auth_rejects_invalid_id_and_expires(
    gateway: tuple[TestClient, FakePipeline, ServerConfig], tmp_path: Path
) -> None:
    client, _pipeline, config = gateway
    body = upload(client, make_wav(tmp_path / "input.wav")).json()
    assert client.get(body["audio_url"]).status_code == 401
    assert client.get("/v1/audio/../../etc/passwd", headers=auth()).status_code == 404
    path = config.temp_audio_dir / f"output-{body['audio_id']}.wav"
    os.utime(path, (time.time() - 301, time.time() - 301))
    assert client.get(body["audio_url"], headers=auth()).status_code == 404
    assert not path.exists()


def test_audio_store_ttl_cleanup(tmp_path: Path) -> None:
    store = AudioStore(tmp_path, ttl_seconds=5)
    old = make_wav(tmp_path / "generated.wav")
    _audio_id, published = store.publish(old)
    os.utime(published, (10, 10))
    assert store.cleanup_expired(now=20) == 1
    assert not published.exists()


def test_concurrency_gate_rejects_excess_and_recovers() -> None:
    async def scenario() -> None:
        gate = ConcurrencyGate(1)
        await gate.enter()
        with pytest.raises(GatewayHTTPError) as error:
            await gate.enter()
        assert error.value.status_code == 429
        await gate.leave()
        await gate.enter()
        await gate.leave()

    asyncio.run(scenario())
