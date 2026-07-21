from pathlib import Path

import pytest

from ai_voice_interpreter.config import AppConfig
from ai_voice_interpreter.exceptions import VoiceEnrollmentError
from ai_voice_interpreter.voice_enrollment import (
    enroll_voice,
    validate_enrollment_arguments,
    write_user_config,
)


class FakeEnrollmentService:
    def create_voice(
        self,
        target_model: str,
        prefix: str,
        url: str,
        language_hints: list[str],
    ) -> str:
        assert url.startswith("https://")
        assert language_hints == ["zh"]
        return f"{target_model}-{prefix}-abcd"

    def get_last_request_id(self) -> str:
        return "request-123"


@pytest.mark.parametrize(
    ("url", "prefix"),
    [
        ("file:///tmp/voice.wav", "voice"),
        ("https://example.com/voice.wav", "prefix-is-too-long"),
        ("https://example.com/voice.wav", "bad_name"),
    ],
)
def test_voice_enrollment_argument_validation(url: str, prefix: str) -> None:
    with pytest.raises(VoiceEnrollmentError):
        validate_enrollment_arguments(url, prefix, "zh")


def test_voice_enrollment_uses_tts_target_model() -> None:
    config = AppConfig(app_mode="real", dashscope_api_key="unit-test-key")
    result = enroll_voice(
        config,
        "https://example.com/voice.wav",
        "myvoice",
        service_factory=FakeEnrollmentService,
    )
    assert result.voice_id.startswith(f"{config.tts_model}-")
    assert result.request_id == "request-123"


def test_write_user_config_is_local_and_updates_existing_values(tmp_path: Path) -> None:
    path = tmp_path / "config.env"
    path.write_text("TTS_MODEL=old\nOTHER=value\n", encoding="utf-8")
    write_user_config("cosyvoice-v3-flash-me-abcd", "cosyvoice-v3-flash", path)
    content = path.read_text(encoding="utf-8")
    assert "TTS_MODEL=cosyvoice-v3-flash" in content
    assert "CLONED_VOICE_ID=cosyvoice-v3-flash-me-abcd" in content
    assert "OTHER=value" in content

