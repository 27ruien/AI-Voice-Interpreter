from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path

from dotenv import dotenv_values

from .exceptions import ConfigurationError

DEFAULT_TTS_MODEL = "cosyvoice-v3-flash"
DEFAULT_SYSTEM_VOICES = {DEFAULT_TTS_MODEL: "longanyang"}
DEFAULT_SYSTEM_VOICE = DEFAULT_SYSTEM_VOICES[DEFAULT_TTS_MODEL]
DEFAULT_HTTP_URLS = {
    "beijing": "https://dashscope.aliyuncs.com/api/v1",
    "singapore": "https://dashscope-intl.aliyuncs.com/api/v1",
}
DEFAULT_WEBSOCKET_URLS = {
    "beijing": "wss://dashscope.aliyuncs.com/api-ws/v1/inference",
    "singapore": "wss://dashscope-intl.aliyuncs.com/api-ws/v1/inference",
}


def _as_bool(value: str | bool | None, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ConfigurationError(f"无法解析布尔配置值：{value}")


@dataclass(frozen=True, slots=True)
class AppConfig:
    app_mode: str = "real"
    interpreter_mode: str = "remote_stream"
    ai_gateway_base_url: str = "https://gridworks.cn/tool/ai-interpreter-api"
    ai_gateway_token: str = field(default="", repr=False)
    dashscope_api_key: str = field(default="", repr=False)
    dashscope_region: str = "beijing"
    dashscope_workspace_id: str = ""
    dashscope_http_base_url: str = ""
    dashscope_websocket_base_url: str = ""
    asr_provider: str = "dashscope"
    asr_model: str = "paraformer-realtime-v2"
    translation_provider: str = "dashscope"
    translation_model: str = "qwen-mt-flash"
    source_language: str = "zh"
    target_language: str = "en"
    tts_provider: str = "dashscope"
    tts_model: str = DEFAULT_TTS_MODEL
    tts_voice: str = ""
    cloned_voice_id: str = ""
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    keep_temp_audio: bool = False
    log_level: str = "INFO"
    network_timeout_seconds: float = 45.0
    stream_audio_chunk_ms: int = 100
    stream_send_queue_max_chunks: int = 50
    stream_ring_buffer_seconds: int = 30
    stream_playback_prebuffer_ms: int = 150
    stream_playback_queue_max_seconds: int = 10
    stream_playback_save_last_turn: bool = True
    stream_capture_mode: str = "safe"
    stream_http_fallback: bool = True

    @classmethod
    def load(
        cls,
        dotenv_path: Path | str = Path(".env"),
        environ: Mapping[str, str] | None = None,
        user_config_path: Path | None = None,
    ) -> AppConfig:
        user_path = Path(user_config_path) if user_config_path else default_user_config_path()
        values: dict[str, str] = {}
        if user_path.exists():
            values.update(_clean_dotenv(dotenv_values(user_path)))
        env_path = Path(dotenv_path)
        if env_path.exists():
            values.update(_clean_dotenv(dotenv_values(env_path)))
        values.update(dict(os.environ if environ is None else environ))

        def get(name: str, default: str) -> str:
            return str(values.get(name, default)).strip()

        try:
            config = cls(
                app_mode=get("APP_MODE", "real").lower(),
                interpreter_mode=get("INTERPRETER_MODE", "remote_stream").lower(),
                ai_gateway_base_url=get(
                    "AI_GATEWAY_BASE_URL",
                    "https://gridworks.cn/tool/ai-interpreter-api",
                ).rstrip("/"),
                ai_gateway_token=get("AI_GATEWAY_TOKEN", ""),
                dashscope_api_key=get("DASHSCOPE_API_KEY", ""),
                dashscope_region=get("DASHSCOPE_REGION", "beijing").lower(),
                dashscope_workspace_id=get("DASHSCOPE_WORKSPACE_ID", ""),
                dashscope_http_base_url=get("DASHSCOPE_HTTP_BASE_URL", ""),
                dashscope_websocket_base_url=get("DASHSCOPE_WEBSOCKET_BASE_URL", ""),
                asr_provider=get("ASR_PROVIDER", "dashscope").lower(),
                asr_model=get("ASR_MODEL", "paraformer-realtime-v2"),
                translation_provider=get("TRANSLATION_PROVIDER", "dashscope").lower(),
                translation_model=get("TRANSLATION_MODEL", "qwen-mt-flash"),
                source_language=get("SOURCE_LANGUAGE", "zh").lower(),
                target_language=get("TARGET_LANGUAGE", "en").lower(),
                tts_provider=get("TTS_PROVIDER", "dashscope").lower(),
                tts_model=get("TTS_MODEL", "cosyvoice-v3-flash"),
                tts_voice=get("TTS_VOICE", ""),
                cloned_voice_id=get("CLONED_VOICE_ID", ""),
                audio_sample_rate=int(get("AUDIO_SAMPLE_RATE", "16000")),
                audio_channels=int(get("AUDIO_CHANNELS", "1")),
                keep_temp_audio=_as_bool(values.get("KEEP_TEMP_AUDIO"), False),
                log_level=get("LOG_LEVEL", "INFO").upper(),
                network_timeout_seconds=float(get("NETWORK_TIMEOUT_SECONDS", "45")),
                stream_audio_chunk_ms=int(get("STREAM_AUDIO_CHUNK_MS", "100")),
                stream_send_queue_max_chunks=int(
                    get("STREAM_SEND_QUEUE_MAX_CHUNKS", "50")
                ),
                stream_ring_buffer_seconds=int(get("STREAM_RING_BUFFER_SECONDS", "30")),
                stream_playback_prebuffer_ms=int(
                    get("STREAM_PLAYBACK_PREBUFFER_MS", "150")
                ),
                stream_playback_queue_max_seconds=int(
                    get("STREAM_PLAYBACK_QUEUE_MAX_SECONDS", "10")
                ),
                stream_playback_save_last_turn=_as_bool(
                    values.get("STREAM_PLAYBACK_SAVE_LAST_TURN"), True
                ),
                stream_capture_mode=get("STREAM_CAPTURE_MODE", "safe").lower(),
                stream_http_fallback=_as_bool(values.get("STREAM_HTTP_FALLBACK"), True),
            )
        except ValueError as exc:
            raise ConfigurationError(f"配置值格式错误：{exc}") from exc
        config.validate_basic()
        return config

    def validate_basic(self) -> None:
        if self.app_mode not in {"real", "mock"}:
            raise ConfigurationError("APP_MODE 必须是 real 或 mock。")
        if self.interpreter_mode not in {"remote", "remote_stream", "local"}:
            raise ConfigurationError(
                "INTERPRETER_MODE 必须是 remote_stream、remote 或 local。"
            )
        if self.dashscope_region not in DEFAULT_HTTP_URLS:
            raise ConfigurationError("DASHSCOPE_REGION 必须是 beijing 或 singapore。")
        if self.audio_sample_rate <= 0 or self.audio_channels != 1:
            raise ConfigurationError("当前 MVP 要求正采样率和 AUDIO_CHANNELS=1。")
        if self.network_timeout_seconds <= 0:
            raise ConfigurationError("NETWORK_TIMEOUT_SECONDS 必须大于 0。")
        if self.stream_audio_chunk_ms <= 0 or self.stream_send_queue_max_chunks <= 0:
            raise ConfigurationError("流式分块和发送队列配置必须大于 0。")
        if self.stream_ring_buffer_seconds <= 0 or self.stream_playback_queue_max_seconds <= 0:
            raise ConfigurationError("流式 Ring Buffer 和播放队列配置必须大于 0。")
        if self.stream_capture_mode not in {"safe", "headphones"}:
            raise ConfigurationError("STREAM_CAPTURE_MODE 必须是 safe 或 headphones。")
        if self.cloned_voice_id and not self.cloned_voice_id.startswith(f"{self.tts_model}-"):
            raise ConfigurationError(
                "CLONED_VOICE_ID 与 TTS_MODEL 不匹配；复刻和合成必须使用同一模型。"
            )
        if (
            not self.cloned_voice_id
            and not self.tts_voice
            and self.tts_model not in DEFAULT_SYSTEM_VOICES
        ):
            raise ConfigurationError(
                "当前 TTS_MODEL 没有内置默认音色，请显式配置兼容的 TTS_VOICE。"
            )

    def validate_for_processing(self) -> None:
        self.validate_basic()
        if self.app_mode == "real" and self.interpreter_mode in {"remote", "remote_stream"}:
            if not self.ai_gateway_base_url.startswith(("http://", "https://")):
                raise ConfigurationError("远程模式的 AI_GATEWAY_BASE_URL 必须是 HTTP(S) 地址。")
            if not self.ai_gateway_token:
                raise ConfigurationError("远程模式缺少 AI_GATEWAY_TOKEN。")
        if self.app_mode == "real" and self.interpreter_mode == "local":
            if not self.dashscope_api_key:
                raise ConfigurationError("本地直连模式缺少 DASHSCOPE_API_KEY。")
            providers = {self.asr_provider, self.translation_provider, self.tts_provider}
            if providers != {"dashscope"}:
                raise ConfigurationError("本地直连模式当前仅支持 dashscope Provider。")

    @property
    def effective_tts_voice(self) -> str:
        if self.cloned_voice_id:
            return self.cloned_voice_id
        if self.tts_voice:
            return self.tts_voice
        try:
            return DEFAULT_SYSTEM_VOICES[self.tts_model]
        except KeyError as exc:
            raise ConfigurationError(
                "当前 TTS_MODEL 没有内置默认音色，请显式配置兼容的 TTS_VOICE。"
            ) from exc

    @property
    def http_base_url(self) -> str:
        if self.dashscope_http_base_url:
            return self.dashscope_http_base_url.rstrip("/")
        if self.dashscope_workspace_id:
            domain = _workspace_domain(self.dashscope_region)
            return f"https://{self.dashscope_workspace_id}.{domain}/api/v1"
        return DEFAULT_HTTP_URLS[self.dashscope_region]

    @property
    def websocket_base_url(self) -> str:
        if self.dashscope_websocket_base_url:
            return self.dashscope_websocket_base_url.rstrip("/")
        if self.dashscope_workspace_id:
            domain = _workspace_domain(self.dashscope_region)
            return f"wss://{self.dashscope_workspace_id}.{domain}/api-ws/v1/inference"
        return DEFAULT_WEBSOCKET_URLS[self.dashscope_region]

    def safe_summary(self) -> dict[str, object]:
        summary = {field.name: getattr(self, field.name) for field in fields(self)}
        summary["dashscope_api_key"] = "***configured***" if self.dashscope_api_key else ""
        summary["ai_gateway_token"] = "***configured***" if self.ai_gateway_token else ""
        return summary


def default_user_config_path() -> Path:
    return Path.home() / ".config" / "ai-voice-interpreter" / "config.env"


def _workspace_domain(region: str) -> str:
    if region == "beijing":
        return "cn-beijing.maas.aliyuncs.com"
    return "ap-southeast-1.maas.aliyuncs.com"


def _clean_dotenv(values: Mapping[str, str | None]) -> dict[str, str]:
    return {key: value for key, value in values.items() if value is not None}
