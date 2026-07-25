from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

from ai_voice_interpreter.config import AppConfig


@dataclass(frozen=True, slots=True)
class ServerConfig:
    app_env: str = "production"
    log_level: str = "INFO"
    dashscope_api_key: str = field(default="", repr=False)
    dashscope_workspace_id: str = ""
    dashscope_native_base_url: str = ""
    dashscope_compatible_base_url: str = ""
    asr_model: str = "paraformer-realtime-v2"
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

        config = cls(
            app_env=get("APP_ENV", "production"),
            log_level=get("LOG_LEVEL", "INFO").upper(),
            dashscope_api_key=get("DASHSCOPE_API_KEY", ""),
            dashscope_workspace_id=get("DASHSCOPE_WORKSPACE_ID", ""),
            dashscope_native_base_url=get("DASHSCOPE_NATIVE_BASE_URL", "").rstrip("/"),
            dashscope_compatible_base_url=get(
                "DASHSCOPE_COMPATIBLE_BASE_URL", ""
            ).rstrip("/"),
            asr_model=get("ASR_MODEL", "paraformer-realtime-v2"),
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

    def provider_config(self) -> AppConfig:
        return AppConfig(
            app_mode="real",
            interpreter_mode="local",
            dashscope_api_key=self.dashscope_api_key,
            dashscope_workspace_id=self.dashscope_workspace_id,
            dashscope_http_base_url=self.dashscope_native_base_url,
            asr_model=self.asr_model,
            translation_model=self.translation_model,
            tts_model=self.tts_model,
            tts_voice=self.tts_voice,
            cloned_voice_id=self.cloned_voice_id,
            network_timeout_seconds=self.request_timeout_seconds,
        )
