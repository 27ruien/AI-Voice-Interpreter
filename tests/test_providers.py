import wave
from pathlib import Path

import pytest

from ai_voice_interpreter.config import AppConfig
from ai_voice_interpreter.exceptions import ConfigurationError
from ai_voice_interpreter.providers.dashscope_tts import DashScopeTextToSpeech
from ai_voice_interpreter.providers.mock_providers import (
    MockSpeechRecognizer,
    MockTextToSpeech,
    MockTranslator,
)


def test_mock_asr_returns_structured_chinese_result(tmp_path: Path) -> None:
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"test")
    result = MockSpeechRecognizer().transcribe(audio)
    assert result.text
    assert result.language == "zh"
    assert result.provider == "mock"
    assert result.duration_ms >= 0


def test_mock_translation_returns_structured_english_result() -> None:
    result = MockTranslator().translate("你好", "zh", "en")
    assert result.source_text == "你好"
    assert result.translated_text.startswith("Hello")
    assert result.target_language == "en"


def test_mock_tts_creates_valid_wav_and_cleanup(tmp_path: Path) -> None:
    output_dir = tmp_path / "tts"
    provider = MockTextToSpeech(output_dir)
    result = provider.synthesize("Hello")
    with wave.open(str(result.audio_path), "rb") as wav_file:
        assert wav_file.getnchannels() == 1
        assert wav_file.getsampwidth() == 2
        assert wav_file.getnframes() > 0
    assert result.audio_format == "wav"
    assert result.duration_ms >= 0


def test_real_tts_refuses_non_clone_voice_when_clone_is_configured(tmp_path: Path) -> None:
    config = AppConfig(
        app_mode="real",
        dashscope_api_key="fake-for-unit-test",
        tts_model="cosyvoice-v3-flash",
        cloned_voice_id="cosyvoice-v3-flash-mine-1234",
    )
    provider = DashScopeTextToSpeech(config, tmp_path)
    with pytest.raises(ConfigurationError, match="未使用"):
        provider._validate_voice("longanyang")

