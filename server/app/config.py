from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from ai_voice_interpreter.config import AppConfig


def _as_bool(value: str, default: bool) -> bool:
    if not value:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"无法解析布尔配置：{value}")


@dataclass(frozen=True, slots=True)
class ServerConfig:
    app_env: str = "production"
    log_level: str = "INFO"
    dashscope_api_key: str = field(default="", repr=False)
    dashscope_workspace_id: str = ""
    dashscope_native_base_url: str = ""
    dashscope_compatible_base_url: str = ""
    asr_model: str = "paraformer-v2"
    stream_asr_model: str = "paraformer-realtime-v2"
    translation_model: str = "qwen-mt-flash"
    tts_model: str = "cosyvoice-v3-flash"
    tts_voice: str = "longanyang"
    cloned_voice_id: str = ""
    client_test_token: str = field(default="", repr=False)
    max_upload_mb: int = 20
    max_concurrent_requests: int = 2
    audio_ttl_seconds: int = 300
    request_timeout_seconds: float = 120.0
    temp_audio_dir: Path = Path("/tmp/ai-voice-interpreter")
    streaming_enabled: bool = True
    streaming_protocol_version: str = "1.0"
    streaming_max_session_seconds: int = 3600
    streaming_max_connections: int = 2
    streaming_max_connections_per_token: int = 1
    streaming_heartbeat_seconds: int = 20
    streaming_heartbeat_timeout_seconds: int = 60
    streaming_max_frame_bytes: int = 65536
    stream_audio_sample_rate: int = 16000
    stream_audio_channels: int = 1
    stream_audio_chunk_ms: int = 100
    stream_audio_queue_max_chunks: int = 50
    vad_enabled: bool = True
    vad_frame_ms: int = 20
    vad_aggressiveness: int = 2
    vad_min_speech_ms: int = 250
    vad_silence_ms: int = 650
    vad_pre_roll_ms: int = 200
    vad_max_turn_ms: int = 15000
    tts_text_min_chars: int = 20
    tts_text_target_chars: int = 48
    tts_text_max_chars: int = 90
    tts_text_max_wait_ms: int = 300
    stream_turn_queue_max: int = 3
    stream_tts_text_queue_max: int = 10
    stream_tts_audio_queue_max_chunks: int = 100
    stream_pipeline_provider: str = "livetranslate"
    stream_pipeline_fallback_provider: str = "modular"
    allow_stream_pipeline_override: bool = False
    livetranslate_model: str = "qwen3.5-livetranslate-flash-realtime"
    livetranslate_source_language: str = "zh"
    livetranslate_target_language: str = "en"
    livetranslate_output_modalities: tuple[str, ...] = ("text", "audio")
    livetranslate_voice: str = "Tina"
    livetranslate_enable_source_transcription: bool = True
    livetranslate_source_asr_model: str = "qwen3-asr-flash-realtime"
    livetranslate_source_transcription_fallback: str = "none"
    livetranslate_enable_voice_clone: bool = False
    livetranslate_voice_clone_frequency: str = "once"
    livetranslate_connect_timeout_seconds: float = 15.0
    livetranslate_session_finish_timeout_seconds: float = 20.0
    livetranslate_audio_queue_max_chunks: int = 100
    livetranslate_hotwords: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(
        cls,
        dotenv_path: Path | str = Path("server/.env"),
        environ: Mapping[str, str] | None = None,
    ) -> ServerConfig:
        values: dict[str, str] = {}
        path = Path(dotenv_path)
        if path.exists():
            values.update({k: v for k, v in dotenv_values(path).items() if v is not None})
        values.update(dict(os.environ if environ is None else environ))

        def get(name: str, default: str) -> str:
            return str(values.get(name, default)).strip()

        def get_json_map(name: str) -> dict[str, str]:
            raw = get(name, "")
            if not raw:
                return {}
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{name} 必须是 JSON 对象。") from exc
            if not isinstance(parsed, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in parsed.items()
            ):
                raise ValueError(f"{name} 必须是字符串到字符串的 JSON 对象。")
            return {key.strip(): value.strip() for key, value in parsed.items() if key.strip()}

        config = cls(
            app_env=get("APP_ENV", "production"),
            log_level=get("LOG_LEVEL", "INFO").upper(),
            dashscope_api_key=get("DASHSCOPE_API_KEY", ""),
            dashscope_workspace_id=get("DASHSCOPE_WORKSPACE_ID", ""),
            dashscope_native_base_url=get("DASHSCOPE_NATIVE_BASE_URL", "").rstrip("/"),
            dashscope_compatible_base_url=get(
                "DASHSCOPE_COMPATIBLE_BASE_URL", ""
            ).rstrip("/"),
            asr_model=get("ASR_MODEL", "paraformer-v2"),
            stream_asr_model=get("STREAM_ASR_MODEL", "paraformer-realtime-v2"),
            translation_model=get("TRANSLATION_MODEL", "qwen-mt-flash"),
            tts_model=get("TTS_MODEL", "cosyvoice-v3-flash"),
            tts_voice=get("TTS_VOICE", "longanyang"),
            cloned_voice_id=get("CLONED_VOICE_ID", ""),
            client_test_token=get("CLIENT_TEST_TOKEN", ""),
            max_upload_mb=int(get("MAX_UPLOAD_MB", "20")),
            max_concurrent_requests=int(get("MAX_CONCURRENT_REQUESTS", "2")),
            audio_ttl_seconds=int(get("AUDIO_TTL_SECONDS", "300")),
            request_timeout_seconds=float(get("REQUEST_TIMEOUT_SECONDS", "120")),
            temp_audio_dir=Path(get("TEMP_AUDIO_DIR", "/tmp/ai-voice-interpreter")),
            streaming_enabled=_as_bool(get("STREAMING_ENABLED", "true"), True),
            streaming_protocol_version=get("STREAMING_PROTOCOL_VERSION", "1.0"),
            streaming_max_session_seconds=int(
                get("STREAMING_MAX_SESSION_SECONDS", "3600")
            ),
            streaming_max_connections=int(get("STREAMING_MAX_CONNECTIONS", "2")),
            streaming_max_connections_per_token=int(
                get("STREAMING_MAX_CONNECTIONS_PER_TOKEN", "1")
            ),
            streaming_heartbeat_seconds=int(get("STREAMING_HEARTBEAT_SECONDS", "20")),
            streaming_heartbeat_timeout_seconds=int(
                get("STREAMING_HEARTBEAT_TIMEOUT_SECONDS", "60")
            ),
            streaming_max_frame_bytes=int(get("STREAMING_MAX_FRAME_BYTES", "65536")),
            stream_audio_sample_rate=int(get("STREAM_AUDIO_SAMPLE_RATE", "16000")),
            stream_audio_channels=int(get("STREAM_AUDIO_CHANNELS", "1")),
            stream_audio_chunk_ms=int(get("STREAM_AUDIO_CHUNK_MS", "100")),
            stream_audio_queue_max_chunks=int(
                get("STREAM_AUDIO_QUEUE_MAX_CHUNKS", "50")
            ),
            vad_enabled=_as_bool(get("VAD_ENABLED", "true"), True),
            vad_frame_ms=int(get("VAD_FRAME_MS", "20")),
            vad_aggressiveness=int(get("VAD_AGGRESSIVENESS", "2")),
            vad_min_speech_ms=int(get("VAD_MIN_SPEECH_MS", "250")),
            vad_silence_ms=int(get("VAD_SILENCE_MS", "650")),
            vad_pre_roll_ms=int(get("VAD_PRE_ROLL_MS", "200")),
            vad_max_turn_ms=int(get("VAD_MAX_TURN_MS", "15000")),
            tts_text_min_chars=int(get("TTS_TEXT_MIN_CHARS", "20")),
            tts_text_target_chars=int(get("TTS_TEXT_TARGET_CHARS", "48")),
            tts_text_max_chars=int(get("TTS_TEXT_MAX_CHARS", "90")),
            tts_text_max_wait_ms=int(get("TTS_TEXT_MAX_WAIT_MS", "300")),
            stream_turn_queue_max=int(get("STREAM_TURN_QUEUE_MAX", "3")),
            stream_tts_text_queue_max=int(get("STREAM_TTS_TEXT_QUEUE_MAX", "10")),
            stream_tts_audio_queue_max_chunks=int(
                get("STREAM_TTS_AUDIO_QUEUE_MAX_CHUNKS", "100")
            ),
            stream_pipeline_provider=get(
                "STREAM_PIPELINE_PROVIDER", "livetranslate"
            ).lower(),
            stream_pipeline_fallback_provider=get(
                "STREAM_PIPELINE_FALLBACK_PROVIDER", "modular"
            ).lower(),
            allow_stream_pipeline_override=_as_bool(
                get("ALLOW_STREAM_PIPELINE_OVERRIDE", "false"), False
            ),
            livetranslate_model=get(
                "LIVETRANSLATE_MODEL", "qwen3.5-livetranslate-flash-realtime"
            ),
            livetranslate_source_language=get(
                "LIVETRANSLATE_SOURCE_LANGUAGE", "zh"
            ).lower(),
            livetranslate_target_language=get(
                "LIVETRANSLATE_TARGET_LANGUAGE", "en"
            ).lower(),
            livetranslate_output_modalities=tuple(
                part.strip().lower()
                for part in get("LIVETRANSLATE_OUTPUT_MODALITIES", "text,audio").split(",")
                if part.strip()
            ),
            livetranslate_voice=get("LIVETRANSLATE_VOICE", "Tina"),
            livetranslate_enable_source_transcription=_as_bool(
                get("LIVETRANSLATE_ENABLE_SOURCE_TRANSCRIPTION", "true"), True
            ),
            livetranslate_source_asr_model=get(
                "LIVETRANSLATE_SOURCE_ASR_MODEL", "qwen3-asr-flash-realtime"
            ),
            livetranslate_source_transcription_fallback=get(
                "LIVETRANSLATE_SOURCE_TRANSCRIPTION_FALLBACK", "none"
            ).lower(),
            livetranslate_enable_voice_clone=_as_bool(
                get("LIVETRANSLATE_ENABLE_VOICE_CLONE", "false"), False
            ),
            livetranslate_voice_clone_frequency=get(
                "LIVETRANSLATE_VOICE_CLONE_FREQUENCY", "once"
            ).lower(),
            livetranslate_connect_timeout_seconds=float(
                get("LIVETRANSLATE_CONNECT_TIMEOUT_SECONDS", "15")
            ),
            livetranslate_session_finish_timeout_seconds=float(
                get("LIVETRANSLATE_SESSION_FINISH_TIMEOUT_SECONDS", "20")
            ),
            livetranslate_audio_queue_max_chunks=int(
                get("LIVETRANSLATE_AUDIO_QUEUE_MAX_CHUNKS", "100")
            ),
            livetranslate_hotwords=get_json_map("LIVETRANSLATE_HOTWORDS_JSON"),
        )
        config.validate()
        return config

    def validate(self) -> None:
        missing = []
        for name, value in (
            ("DASHSCOPE_API_KEY", self.dashscope_api_key),
            ("DASHSCOPE_WORKSPACE_ID", self.dashscope_workspace_id),
            ("DASHSCOPE_NATIVE_BASE_URL", self.dashscope_native_base_url),
            ("CLIENT_TEST_TOKEN", self.client_test_token),
        ):
            if not value:
                missing.append(name)
        if missing:
            raise ValueError(f"缺少服务器配置：{', '.join(missing)}")
        if self.max_upload_mb <= 0 or self.max_concurrent_requests <= 0:
            raise ValueError("上传大小和并发数必须大于 0。")
        if self.audio_ttl_seconds <= 0 or self.request_timeout_seconds <= 0:
            raise ValueError("TTL 和请求超时必须大于 0。")
        positive_stream_values = (
            self.streaming_max_session_seconds,
            self.streaming_max_connections,
            self.streaming_max_connections_per_token,
            self.streaming_heartbeat_seconds,
            self.streaming_heartbeat_timeout_seconds,
            self.streaming_max_frame_bytes,
            self.stream_audio_chunk_ms,
            self.stream_audio_queue_max_chunks,
            self.vad_min_speech_ms,
            self.vad_silence_ms,
            self.vad_max_turn_ms,
            self.tts_text_max_wait_ms,
            self.stream_turn_queue_max,
            self.stream_tts_text_queue_max,
            self.stream_tts_audio_queue_max_chunks,
            self.livetranslate_connect_timeout_seconds,
            self.livetranslate_session_finish_timeout_seconds,
            self.livetranslate_audio_queue_max_chunks,
        )
        if any(value <= 0 for value in positive_stream_values):
            raise ValueError("流式容量和超时配置必须大于 0。")
        if self.streaming_heartbeat_timeout_seconds <= self.streaming_heartbeat_seconds:
            raise ValueError("心跳超时必须大于心跳间隔。")
        if self.streaming_protocol_version != "1.0":
            raise ValueError("当前服务器仅支持 STREAMING_PROTOCOL_VERSION=1.0。")
        if self.streaming_enabled and not self.vad_enabled:
            raise ValueError("当前 Turn-based Streaming 必须启用 VAD。")
        if self.stream_audio_sample_rate != 16000 or self.stream_audio_channels != 1:
            raise ValueError("流式输入当前要求 16 kHz 单声道。")
        if self.vad_frame_ms not in {10, 20, 30} or not 0 <= self.vad_aggressiveness <= 3:
            raise ValueError("VAD_FRAME_MS 必须是 10/20/30，VAD_AGGRESSIVENESS 必须是 0–3。")
        if self.stream_audio_chunk_ms % self.vad_frame_ms:
            raise ValueError("STREAM_AUDIO_CHUNK_MS 必须能被 VAD_FRAME_MS 整除。")
        if self.vad_pre_roll_ms < 0:
            raise ValueError("VAD_PRE_ROLL_MS 不能为负数。")
        if not (
            0 < self.tts_text_min_chars
            <= self.tts_text_target_chars
            <= self.tts_text_max_chars
        ):
            raise ValueError("TTS 文本分段字符阈值顺序无效。")
        if self.livetranslate_output_modalities not in {("text",), ("text", "audio")}:
            raise ValueError(
                "LIVETRANSLATE_OUTPUT_MODALITIES 必须是 text 或 text,audio。"
            )
        if (
            self.livetranslate_source_language != "zh"
            or self.livetranslate_target_language != "en"
        ):
            raise ValueError("当前 LiveTranslate 仅支持配置为中文到英文。")
        if self.livetranslate_source_transcription_fallback != "none":
            raise ValueError("本轮 LIVETRANSLATE_SOURCE_TRANSCRIPTION_FALLBACK 必须是 none。")
        if self.livetranslate_voice_clone_frequency != "once":
            raise ValueError("本轮声音复刻频率仅允许 once。")
        if self.livetranslate_enable_voice_clone and self.livetranslate_voice != "default":
            raise ValueError("启用 once 声音复刻时 LIVETRANSLATE_VOICE 必须是 default。")

    @property
    def stream_provider_configuration_error(self) -> str | None:
        if self.stream_pipeline_provider not in {"livetranslate", "modular"}:
            return "STREAM_PIPELINE_PROVIDER 必须是 livetranslate 或 modular。"
        if self.stream_pipeline_fallback_provider not in {"modular", "none"}:
            return "STREAM_PIPELINE_FALLBACK_PROVIDER 必须是 modular 或 none。"
        if (
            self.stream_pipeline_provider == "modular"
            and self.stream_pipeline_fallback_provider == "modular"
        ):
            return None
        return None

    def provider_config(self, *, asr_model: str | None = None) -> AppConfig:
        return AppConfig(
            app_mode="real",
            interpreter_mode="local",
            dashscope_api_key=self.dashscope_api_key,
            dashscope_workspace_id=self.dashscope_workspace_id,
            dashscope_http_base_url=self.dashscope_native_base_url,
            asr_model=asr_model or self.asr_model,
            translation_model=self.translation_model,
            tts_model=self.tts_model,
            tts_voice=self.tts_voice,
            cloned_voice_id=self.cloned_voice_id,
            network_timeout_seconds=self.request_timeout_seconds,
        )
