import json
import wave
from pathlib import Path

import httpx
import pytest

from ai_voice_interpreter.exceptions import GatewayError
from ai_voice_interpreter.models import ProcessingStatus
from ai_voice_interpreter.remote.gateway_client import GatewayClient
from ai_voice_interpreter.remote.pipeline import RemoteInterpreterPipeline
from ai_voice_interpreter.remote_smoke import _verify_semantics


def make_wav(path: Path) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(16000)
        wav.writeframes(b"\0\0" * 160)
    return path


def payload() -> dict[str, object]:
    return {
        "request_id": "00000000-0000-0000-0000-000000000001",
        "recognized_text": "你好",
        "translated_text": "Hello",
        "audio_id": "00000000-0000-0000-0000-000000000002",
        "audio_url": "/v1/audio/00000000-0000-0000-0000-000000000002",
        "latency": {"asr_ms": 1, "translation_ms": 2, "tts_ms": 3, "total_ms": 6},
        "usage": {"input_audio_seconds": 0.1},
        "models": {"asr": "a", "translation": "b", "tts": "c", "voice": "v"},
        "provider_request_ids": {"asr": "asr-id", "translation": "mt-id", "tts": "tts-id"},
    }


def test_gateway_client_sends_bearer_multipart_and_downloads_wav(tmp_path: Path) -> None:
    input_wav = make_wav(tmp_path / "input.wav")
    output_wav = make_wav(tmp_path / "fixture.wav").read_bytes()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["authorization"] == "Bearer test-token"
        if request.method == "POST":
            assert "multipart/form-data" in request.headers["content-type"]
            assert b'recording.wav' in request.read()
            return httpx.Response(200, json=payload())
        return httpx.Response(200, content=output_wav, headers={"content-type": "audio/wav"})

    client = GatewayClient(
        "https://example.test/tool/api", "test-token", transport=httpx.MockTransport(handler)
    )
    response = client.interpret(input_wav)
    downloaded = client.download_audio(response, tmp_path / "download.wav")
    assert response.recognized_text == "你好"
    assert response.provider_request_ids["tts"] == "tts-id"
    assert downloaded.size_bytes == len(output_wav)
    assert len(requests) == 2
    assert requests[1].url.path.endswith(f"/tool/api{response.audio_url}")


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "凭证无效"), (413, "文件过大"), (429, "并发已满"), (500, "暂时不可用")],
)
def test_gateway_client_maps_http_errors(tmp_path: Path, status: int, expected: str) -> None:
    audio = make_wav(tmp_path / "input.wav")
    body = {"request_id": "request-1", "error": {"message": "raw detail"}}
    transport = httpx.MockTransport(lambda _request: httpx.Response(status, json=body))
    with pytest.raises(GatewayError, match=expected):
        GatewayClient("https://example.test", "token", transport=transport).interpret(audio)


def test_gateway_client_maps_timeout(tmp_path: Path) -> None:
    audio = make_wav(tmp_path / "input.wav")

    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(GatewayError, match="超时"):
        GatewayClient(
            "https://example.test", "token", transport=httpx.MockTransport(timeout)
        ).interpret(audio)


def test_remote_pipeline_statuses_and_cleanup(tmp_path: Path) -> None:
    input_wav = make_wav(tmp_path / "input.wav")
    output_wav = make_wav(tmp_path / "fixture.wav").read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            request.read()
            return httpx.Response(200, json=payload())
        return httpx.Response(200, content=output_wav, headers={"content-type": "audio/wav"})

    output = tmp_path / "remote"
    pipeline = RemoteInterpreterPipeline(
        GatewayClient(
            "https://example.test", "token", transport=httpx.MockTransport(handler)
        ),
        output_dir=output,
    )
    statuses: list[ProcessingStatus] = []
    result = pipeline.process(input_wav, statuses.append)
    assert result.succeeded
    assert statuses == [
        ProcessingStatus.UPLOADING,
        ProcessingStatus.SERVER_PROCESSING,
        ProcessingStatus.DOWNLOADING,
    ]
    assert result.gateway_request_id
    assert result.generated_audio_path and result.generated_audio_path.is_file()


def test_gateway_response_does_not_require_or_expose_token(tmp_path: Path) -> None:
    audio = make_wav(tmp_path / "input.wav")
    response = httpx.Response(200, content=json.dumps(payload()).encode())
    assert "token" not in response.text.lower()
    assert audio.is_file()


@pytest.mark.parametrize(
    "text",
    [
        "Hello, today we will mainly discuss the project progress and the next delivery plan.",
        "Today, our discussion will focus on project status and subsequent delivery arrangements.",
        "Hello, today we’ll primarily discuss the project’s progress and the plan for the next "
        "phase of delivery.",
    ],
)
def test_remote_smoke_semantic_check_accepts_equivalent_wording(text: str) -> None:
    _verify_semantics(text)
