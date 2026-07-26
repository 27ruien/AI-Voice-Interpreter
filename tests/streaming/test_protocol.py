import json
import uuid

import pytest

from ai_voice_interpreter.streaming.protocol import (
    ErrorCode,
    ProtocolError,
    SessionStart,
    error_event,
    parse_control_message,
)


def valid_start() -> dict[str, object]:
    return {
        "type": "session.start",
        "protocol_version": "1.0",
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


def test_valid_session_start_round_trips() -> None:
    parsed = SessionStart.parse(json.dumps(valid_start()))
    assert parsed.audio.sample_rate == 16000
    assert parsed.to_message()["type"] == "session.start"


def test_session_start_round_trips_voice_mode_and_internal_provider_override() -> None:
    payload = valid_start()
    payload["voice_mode"] = "clone_once"
    payload["pipeline_provider"] = "modular"
    payload["source_transcription_enabled"] = False
    parsed = SessionStart.parse(payload)
    rendered = parsed.to_message()
    assert rendered["voice_mode"] == "clone_once"
    assert rendered["pipeline_provider"] == "modular"
    assert rendered["source_transcription_enabled"] is False


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (("protocol_version", "2.0"), ErrorCode.PROTOCOL_VERSION_UNSUPPORTED),
        (("request_id", "bad"), ErrorCode.INVALID_SESSION_START),
    ],
)
def test_session_start_rejects_invalid_fields(
    change: tuple[str, str], code: ErrorCode
) -> None:
    payload = valid_start()
    payload[change[0]] = change[1]
    with pytest.raises(ProtocolError) as error:
        SessionStart.parse(payload)
    assert error.value.code == code


def test_session_start_rejects_invalid_audio() -> None:
    payload = valid_start()
    payload["audio"] = {"format": "wav", "sample_rate": 8000, "channels": 2, "chunk_ms": 0}
    with pytest.raises(ProtocolError) as error:
        SessionStart.parse(payload)
    assert error.value.code == ErrorCode.INVALID_AUDIO_FORMAT


def test_unknown_control_message_is_rejected() -> None:
    with pytest.raises(ProtocolError):
        parse_control_message('{"type":"surprise"}')
    assert parse_control_message('{"type":"session.stop"}')["type"] == "session.stop"


def test_error_event_has_stable_identifiers() -> None:
    event = error_event(
        session_id="session",
        request_id="request",
        turn_id="turn",
        code=ErrorCode.ASR_STREAM_FAILED,
        message="failed",
    )
    assert event["session_id"] == "session"
    assert event["turn_id"] == "turn"
    assert event["code"] == "ASR_STREAM_FAILED"
