from pathlib import Path

import pytest

from ai_voice_interpreter.config import DEFAULT_SYSTEM_VOICE, AppConfig
from ai_voice_interpreter.exceptions import ConfigurationError


def load_isolated(tmp_path: Path, environ: dict[str, str]) -> AppConfig:
    return AppConfig.load(
        dotenv_path=tmp_path / ".env",
        environ=environ,
        user_config_path=tmp_path / "user.env",
    )


def test_config_loads_dotenv_and_environment_takes_precedence(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("APP_MODE=mock\nAUDIO_SAMPLE_RATE=8000\n", encoding="utf-8")
    config = AppConfig.load(
        dotenv_path=tmp_path / ".env",
        environ={"AUDIO_SAMPLE_RATE": "16000"},
        user_config_path=tmp_path / "missing.env",
    )
    assert config.app_mode == "mock"
    assert config.audio_sample_rate == 16000


def test_missing_api_key_allows_load_but_blocks_real_processing(tmp_path: Path) -> None:
    config = load_isolated(tmp_path, {})
    assert config.dashscope_api_key == ""
    with pytest.raises(ConfigurationError, match="DASHSCOPE_API_KEY"):
        config.validate_for_processing()


def test_mock_mode_needs_no_api_key(tmp_path: Path) -> None:
    config = load_isolated(tmp_path, {"APP_MODE": "mock"})
    config.validate_for_processing()


def test_cloned_voice_takes_precedence_over_system_voice(tmp_path: Path) -> None:
    voice_id = "cosyvoice-v3-flash-myvoice-1234"
    config = load_isolated(
        tmp_path,
        {
            "APP_MODE": "mock",
            "TTS_MODEL": "cosyvoice-v3-flash",
            "TTS_VOICE": "longanyang",
            "CLONED_VOICE_ID": voice_id,
        },
    )
    assert config.effective_tts_voice == voice_id


def test_default_system_voice_keeps_non_clone_mode_runnable(tmp_path: Path) -> None:
    config = load_isolated(tmp_path, {"APP_MODE": "mock", "TTS_VOICE": ""})
    assert config.effective_tts_voice == DEFAULT_SYSTEM_VOICE


def test_mismatched_cloned_voice_model_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="不匹配"):
        load_isolated(
            tmp_path,
            {
                "APP_MODE": "mock",
                "TTS_MODEL": "cosyvoice-v3-flash",
                "CLONED_VOICE_ID": "cosyvoice-v2-myvoice-1234",
            },
        )


def test_workspace_builds_beijing_dedicated_endpoints(tmp_path: Path) -> None:
    config = load_isolated(
        tmp_path,
        {"APP_MODE": "mock", "DASHSCOPE_WORKSPACE_ID": "ws123"},
    )
    assert config.http_base_url == "https://ws123.cn-beijing.maas.aliyuncs.com/api/v1"
    assert config.websocket_base_url.startswith("wss://ws123.cn-beijing.maas.aliyuncs.com/")


def test_nondefault_tts_model_requires_explicit_compatible_voice() -> None:
    config = AppConfig(app_mode="mock", tts_model="cosyvoice-v2")
    with pytest.raises(ConfigurationError, match="TTS_VOICE"):
        config.validate_basic()
