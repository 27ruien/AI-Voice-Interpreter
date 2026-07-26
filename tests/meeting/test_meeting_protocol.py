from __future__ import annotations

import uuid

import pytest

from ai_voice_interpreter.streaming.protocol import ErrorCode, ProtocolError, SessionStart


def meeting_start(role: str = "local_to_remote") -> dict[str, object]:
    source, target = ("zh", "en") if role == "local_to_remote" else ("en", "zh")
    return {
        "type": "session.start",
        "protocol_version": "1.1",
        "request_id": str(uuid.uuid4()),
        "bridge_id": str(uuid.uuid4()),
        "session_role": role,
        "source_language": source,
        "target_language": target,
        "mode": "meeting_bridge",
        "voice_mode": "standard",
        "voice": "Tina",
        "audio": {
            "format": "pcm_s16le",
            "sample_rate": 16000,
            "channels": 1,
            "chunk_ms": 100,
        },
        "client": {"platform": "macos", "app_version": "test"},
    }


@pytest.mark.parametrize("role", ["local_to_remote", "remote_to_local"])
def test_protocol_11_round_trips_both_meeting_directions(role: str) -> None:
    parsed = SessionStart.parse(meeting_start(role))
    message = parsed.to_message()
    assert parsed.is_meeting_bridge
    assert message["session_role"] == role
    assert message["bridge_id"]


@pytest.mark.parametrize(
    "change,code",
    [
        (("bridge_id", "bad"), ErrorCode.INVALID_BRIDGE_ID),
        (("session_role", "third"), ErrorCode.INVALID_SESSION_ROLE),
        (("mode", "turn_stream"), ErrorCode.INVALID_SESSION_START),
    ],
)
def test_protocol_11_rejects_invalid_bridge_fields(
    change: tuple[str, str], code: ErrorCode
) -> None:
    payload = meeting_start()
    payload[change[0]] = change[1]
    with pytest.raises(ProtocolError) as error:
        SessionStart.parse(payload)
    assert error.value.code == code


def test_direction_and_languages_must_match() -> None:
    payload = meeting_start("remote_to_local")
    payload["source_language"] = "zh"
    payload["target_language"] = "en"
    with pytest.raises(ProtocolError) as error:
        SessionStart.parse(payload)
    assert error.value.code == ErrorCode.INVALID_SESSION_ROLE


def test_remote_to_local_cannot_request_voice_clone() -> None:
    payload = meeting_start("remote_to_local")
    payload["voice_mode"] = "clone_once"
    with pytest.raises(ProtocolError) as error:
        SessionStart.parse(payload)
    assert error.value.code == ErrorCode.INVALID_SESSION_START


def test_protocol_10_rejects_meeting_fields() -> None:
    payload = meeting_start()
    payload["protocol_version"] = "1.0"
    payload["mode"] = "turn_stream"
    with pytest.raises(ProtocolError):
        SessionStart.parse(payload)
