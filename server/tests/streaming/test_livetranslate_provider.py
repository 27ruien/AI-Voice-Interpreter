from __future__ import annotations

import asyncio
import base64
import json
import ssl

import pytest

from server.app.config import ServerConfig
from server.app.providers.livetranslate import (
    LiveTranslateProviderError,
    LiveTranslateSessionOptions,
    LiveTranslateUpstreamSession,
    TranscriptNormalizer,
    authorization_headers,
    build_audio_append,
    build_livetranslate_endpoint,
    build_session_update,
    decode_audio_delta,
    output_pcm_spec,
)


def settings(**overrides: object) -> ServerConfig:
    values: dict[str, object] = {
        "dashscope_api_key": "unit-secret",
        "dashscope_workspace_id": "workspace-123",
        "dashscope_native_base_url": "https://workspace.example/api/v1",
        "client_test_token": "token",
    }
    values.update(overrides)
    return ServerConfig(**values)


def test_endpoint_replaces_workspace_and_adds_model_query_once() -> None:
    endpoint = build_livetranslate_endpoint("workspace-123", "model/name")
    assert endpoint.startswith(
        "wss://workspace-123.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?"
    )
    assert endpoint.endswith("model=model%2Fname")
    assert endpoint.count("/api-ws/v1/realtime") == 1


@pytest.mark.parametrize("workspace,model", [("", "model"), ("workspace", "")])
def test_endpoint_rejects_missing_configuration(workspace: str, model: str) -> None:
    with pytest.raises(LiveTranslateProviderError, match="CONFIG_MISSING"):
        build_livetranslate_endpoint(workspace, model)


def test_authorization_header_and_missing_key() -> None:
    assert authorization_headers("secret") == {"Authorization": "Bearer secret"}
    with pytest.raises(LiveTranslateProviderError, match="CONFIG_MISSING"):
        authorization_headers("")


def test_standard_session_update_is_zh_to_en_text_audio() -> None:
    payload = build_session_update(settings(), LiveTranslateSessionOptions())
    session = payload["session"]
    assert payload["type"] == "session.update"
    assert session["modalities"] == ["text", "audio"]
    assert session["voice"] == "Tina"
    assert session["input_audio_transcription"] == {
        "model": "qwen3-asr-flash-realtime",
        "language": "zh",
    }
    assert session["translation"] == {"language": "en"}
    assert session["enable_voice_clone"] is False
    assert "voice_clone_options" not in session


def test_source_transcription_can_be_disabled_per_session() -> None:
    payload = build_session_update(
        settings(), LiveTranslateSessionOptions(source_transcription_enabled=False)
    )
    assert "input_audio_transcription" not in payload["session"]


def test_clone_once_uses_default_voice_and_once_frequency() -> None:
    payload = build_session_update(
        settings(), LiveTranslateSessionOptions(voice_mode="clone_once")
    )
    session = payload["session"]
    assert session["enable_voice_clone"] is True
    assert session["voice"] == "default"
    assert session["voice_clone_options"] == {"frequency": "once"}


def test_clone_parameter_conflict_is_rejected() -> None:
    with pytest.raises(LiveTranslateProviderError, match="once"):
        build_session_update(
            settings(livetranslate_voice_clone_frequency="always"),
            LiveTranslateSessionOptions(voice_mode="clone_once"),
        )


def test_hotwords_are_omitted_when_empty_and_loaded_when_present() -> None:
    empty = build_session_update(settings(), LiveTranslateSessionOptions())
    assert "corpus" not in empty["session"]["translation"]
    configured = build_session_update(
        settings(
            livetranslate_hotwords={
                "项目进度": "project progress",
                "交付计划": "delivery plan",
            }
        ),
        LiveTranslateSessionOptions(),
    )
    assert configured["session"]["translation"]["corpus"]["phrases"] == {
        "项目进度": "project progress",
        "交付计划": "delivery plan",
    }


def test_audio_append_encodes_pcm_without_logging_payload() -> None:
    pcm = b"\x01\x00\x02\x00"
    payload = build_audio_append(pcm)
    assert payload["type"] == "input_audio_buffer.append"
    assert base64.b64decode(payload["audio"]) == pcm
    assert payload["event_id"].startswith("event_")


@pytest.mark.parametrize("pcm", [b"", b"\x01"])
def test_audio_append_rejects_empty_or_odd_pcm(pcm: bytes) -> None:
    with pytest.raises(LiveTranslateProviderError):
        build_audio_append(pcm)


def test_audio_delta_decodes_and_rejects_invalid_values() -> None:
    pcm = b"\x00\x00" * 10
    assert decode_audio_delta(base64.b64encode(pcm).decode()) == pcm
    for value in ("", "not base64!", base64.b64encode(b"x").decode()):
        with pytest.raises(LiveTranslateProviderError):
            decode_audio_delta(value)


def test_output_pcm_spec_is_derived_from_session_response() -> None:
    assert output_pcm_spec("pcm24").sample_rate == 24000
    with pytest.raises(LiveTranslateProviderError, match="UNSUPPORTED_OUTPUT_AUDIO_FORMAT"):
        output_pcm_spec("pcm16")


@pytest.mark.parametrize(
    "updates,final,expected",
    [
        ([("Hello", " world"), ("Hello world", "!")], "Hello world!", "Hello world!"),
        ([("Hello", " wrld"), ("Hello", " world")], "Hello world", "Hello world"),
        ([("项目", "进度"), ("项目进度", "")], "项目进度。", "项目进度。"),
        ([("I'm", " here")], "I'm here", "I'm here"),
    ],
)
def test_transcript_normalizer_handles_stash_correction_and_unicode(
    updates: list[tuple[str, str]], final: str, expected: str
) -> None:
    normalizer = TranscriptNormalizer()
    displays = [normalizer.update(text, stash) for text, stash in updates]
    assert [display for display in displays if display][-1] == (
        updates[-1][0] + updates[-1][1]
    )
    assert normalizer.complete(final) == expected
    assert normalizer.complete("duplicate") is None
    assert normalizer.update("late", "event") is None


def test_transcript_normalizer_deduplicates_empty_and_duplicate_events() -> None:
    normalizer = TranscriptNormalizer()
    assert normalizer.update("", "") is None
    assert normalizer.update("Hello", "") == "Hello"
    assert normalizer.update("Hello", "") is None
    assert normalizer.complete("") == "Hello"


class FakeConnection:
    def __init__(self) -> None:
        self.incoming = asyncio.Queue()
        self.incoming.put_nowait(
            json.dumps(
                {
                    "type": "session.created",
                    "event_id": "event-created",
                    "session": {"id": "session-id"},
                }
            )
        )
        self.incoming.put_nowait(
            json.dumps(
                {
                    "type": "session.updated",
                    "event_id": "event-updated",
                    "session": {
                        "id": "session-id",
                        "model": "qwen3.5-livetranslate-flash-realtime",
                        "output_audio_format": "pcm24",
                    },
                }
            )
        )
        self.sent: list[dict[str, object]] = []
        self.closed = False

    async def recv(self) -> str:
        return await self.incoming.get()

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True

    def __aiter__(self):  # type: ignore[no-untyped-def]
        return self

    async def __anext__(self) -> str:
        await asyncio.Event().wait()
        raise StopAsyncIteration


@pytest.mark.anyio
async def test_upstream_connect_enables_tls_and_cleans_tasks() -> None:
    connection = FakeConnection()
    captured: dict[str, object] = {}

    async def factory(_endpoint: str, **kwargs: object) -> FakeConnection:
        captured.update(kwargs)
        return connection

    upstream = LiveTranslateUpstreamSession(
        settings(), LiveTranslateSessionOptions(), connect_factory=factory
    )
    before = LiveTranslateUpstreamSession.active_connections
    await upstream.start()
    tls = captured["ssl"]
    assert isinstance(tls, ssl.SSLContext)
    assert tls.check_hostname is True
    assert tls.verify_mode == ssl.CERT_REQUIRED
    await upstream.send_audio(b"\x00\x00" * 20)
    await asyncio.sleep(0)
    await upstream.cancel()
    assert connection.closed is True
    assert LiveTranslateUpstreamSession.active_connections == before
    assert upstream._sender_task is None or upstream._sender_task.done()  # noqa: SLF001
    assert upstream._receiver_task is None or upstream._receiver_task.done()  # noqa: SLF001


@pytest.mark.anyio
async def test_upstream_audio_queue_is_bounded_and_preserves_order() -> None:
    connection = FakeConnection()

    async def factory(_endpoint: str, **_kwargs: object) -> FakeConnection:
        return connection

    upstream = LiveTranslateUpstreamSession(
        settings(livetranslate_audio_queue_max_chunks=2),
        LiveTranslateSessionOptions(),
        connect_factory=factory,
    )
    await upstream.start()
    chunks = [b"\x01\x00" * 4, b"\x02\x00" * 4]
    for chunk in chunks:
        await upstream.send_audio(chunk)
    await asyncio.sleep(0.01)
    encoded = [event for event in connection.sent if event["type"] == "input_audio_buffer.append"]
    assert [base64.b64decode(str(event["audio"])) for event in encoded] == chunks
    await upstream.cancel()
