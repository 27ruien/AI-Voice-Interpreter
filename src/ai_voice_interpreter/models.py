from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class ProcessingStatus(StrEnum):
    READY = "就绪"
    RECORDING = "正在录音"
    UPLOADING = "正在上传"
    SERVER_PROCESSING = "服务器处理中"
    DOWNLOADING = "正在下载语音"
    RECOGNIZING = "正在识别"
    TRANSLATING = "正在翻译"
    SYNTHESIZING = "正在生成语音"
    PLAYING = "正在播放"
    COMPLETED = "完成"
    FAILED = "失败"


@dataclass(frozen=True, slots=True)
class ASRResult:
    text: str
    language: str
    duration_ms: float
    provider: str
    model: str
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class TranslationResult:
    source_text: str
    translated_text: str
    source_language: str
    target_language: str
    duration_ms: float
    provider: str
    model: str
    request_id: str | None = None


@dataclass(frozen=True, slots=True)
class TTSResult:
    audio_path: Path
    audio_format: str
    duration_ms: float
    provider: str
    model: str
    voice: str
    request_id: str | None = None


@dataclass(slots=True)
class PipelineResult:
    recognized_text: str = ""
    translated_text: str = ""
    generated_audio_path: Path | None = None
    asr_latency_ms: float = 0.0
    translation_latency_ms: float = 0.0
    tts_latency_ms: float = 0.0
    total_latency_ms: float = 0.0
    providers: dict[str, str] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)
    request_ids: dict[str, str] = field(default_factory=dict)
    gateway_request_id: str | None = None
    network_latency_ms: dict[str, float] = field(default_factory=dict)
    error: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.generated_audio_path is not None
