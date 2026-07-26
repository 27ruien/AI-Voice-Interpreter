from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

PROTOCOL_VERSION = "1.0"
MEETING_PROTOCOL_VERSION = "1.1"
SUPPORTED_PROTOCOL_VERSIONS = {PROTOCOL_VERSION, MEETING_PROTOCOL_VERSION}
CLIENT_MESSAGE_TYPES = {"session.start", "session.stop", "ping"}


class ErrorCode(StrEnum):
    AUTH_FAILED = "AUTH_FAILED"
    PROTOCOL_VERSION_UNSUPPORTED = "PROTOCOL_VERSION_UNSUPPORTED"
    INVALID_SESSION_START = "INVALID_SESSION_START"
    INVALID_AUDIO_FORMAT = "INVALID_AUDIO_FORMAT"
    AUDIO_FRAME_TOO_LARGE = "AUDIO_FRAME_TOO_LARGE"
    SESSION_LIMIT_REACHED = "SESSION_LIMIT_REACHED"
    SESSION_TIMEOUT = "SESSION_TIMEOUT"
    HEARTBEAT_TIMEOUT = "HEARTBEAT_TIMEOUT"
    CLIENT_BACKPRESSURE = "CLIENT_BACKPRESSURE"
    SERVER_BACKPRESSURE = "SERVER_BACKPRESSURE"
    ASR_CONNECTION_FAILED = "ASR_CONNECTION_FAILED"
    ASR_STREAM_FAILED = "ASR_STREAM_FAILED"
    ASR_EMPTY_RESULT = "ASR_EMPTY_RESULT"
    TRANSLATION_FAILED = "TRANSLATION_FAILED"
    TRANSLATION_EMPTY_RESULT = "TRANSLATION_EMPTY_RESULT"
    TTS_STREAM_FAILED = "TTS_STREAM_FAILED"
    TTS_AUDIO_INVALID = "TTS_AUDIO_INVALID"
    PLAYBACK_FAILED = "PLAYBACK_FAILED"
    FALLBACK_REQUIRED = "FALLBACK_REQUIRED"
    PIPELINE_CONFIGURATION_INVALID = "PIPELINE_CONFIGURATION_INVALID"
    PIPELINE_OVERRIDE_DISABLED = "PIPELINE_OVERRIDE_DISABLED"
    LIVETRANSLATE_CONNECTION_FAILED = "LIVETRANSLATE_CONNECTION_FAILED"
    LIVETRANSLATE_STREAM_FAILED = "LIVETRANSLATE_STREAM_FAILED"
    LIVETRANSLATE_FINISH_TIMEOUT = "LIVETRANSLATE_FINISH_TIMEOUT"
    LIVETRANSLATE_AUDIO_INVALID = "LIVETRANSLATE_AUDIO_INVALID"
    SOURCE_TRANSCRIPTION_UNAVAILABLE = "SOURCE_TRANSCRIPTION_UNAVAILABLE"
    VOICE_CLONE_FAILED = "VOICE_CLONE_FAILED"
    MEETING_BRIDGE_DISABLED = "MEETING_BRIDGE_DISABLED"
    INVALID_BRIDGE_ID = "INVALID_BRIDGE_ID"
    INVALID_SESSION_ROLE = "INVALID_SESSION_ROLE"
    BRIDGE_ROLE_CONFLICT = "BRIDGE_ROLE_CONFLICT"
    BRIDGE_LIMIT_REACHED = "BRIDGE_LIMIT_REACHED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ProtocolError(ValueError):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class AudioInputSpec:
    format: str
    sample_rate: int
    channels: int
    chunk_ms: int


@dataclass(frozen=True, slots=True)
class SessionStart:
    request_id: str
    source_language: str
    target_language: str
    mode: str
    voice: str | None
    audio: AudioInputSpec
    client_platform: str
    app_version: str
    pipeline_provider: str | None = None
    voice_mode: str = "standard"
    source_transcription_enabled: bool = True
    protocol_version: str = PROTOCOL_VERSION
    bridge_id: str | None = None
    session_role: str | None = None

    @classmethod
    def parse(cls, payload: str | dict[str, Any]) -> SessionStart:
        try:
            data = json.loads(payload) if isinstance(payload, str) else payload
        except json.JSONDecodeError as exc:
            raise ProtocolError(ErrorCode.INVALID_SESSION_START, "控制消息不是有效 JSON。") from exc
        if not isinstance(data, dict) or data.get("type") != "session.start":
            raise ProtocolError(ErrorCode.INVALID_SESSION_START, "首条消息必须是 session.start。")
        version = str(data.get("protocol_version", ""))
        if version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise ProtocolError(
                ErrorCode.PROTOCOL_VERSION_UNSUPPORTED,
                f"不支持的协议版本：{version or 'missing'}。",
            )
        try:
            request_id = str(uuid.UUID(str(data["request_id"])))
            audio = data["audio"]
            client = data["client"]
            spec = AudioInputSpec(
                format=str(audio["format"]),
                sample_rate=int(audio["sample_rate"]),
                channels=int(audio["channels"]),
                chunk_ms=int(audio["chunk_ms"]),
            )
            start = cls(
                request_id=request_id,
                source_language=str(data.get("source_language", "zh")),
                target_language=str(data.get("target_language", "en")),
                mode=str(data.get("mode", "turn_stream")),
                voice=str(data["voice"]) if data.get("voice") else None,
                audio=spec,
                client_platform=str(client["platform"]),
                app_version=str(client["app_version"]),
                pipeline_provider=(
                    str(data["pipeline_provider"]).lower()
                    if data.get("pipeline_provider")
                    else None
                ),
                voice_mode=str(data.get("voice_mode", "standard")).lower(),
                source_transcription_enabled=bool(
                    data.get("source_transcription_enabled", True)
                ),
                protocol_version=version,
                bridge_id=(str(data["bridge_id"]) if data.get("bridge_id") else None),
                session_role=(
                    str(data["session_role"]).lower()
                    if data.get("session_role")
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ProtocolError(
                ErrorCode.INVALID_SESSION_START, "session.start 缺少或包含无效字段。"
            ) from exc
        start.validate()
        return start

    def validate(self) -> None:
        if self.protocol_version == PROTOCOL_VERSION:
            if self.source_language != "zh" or self.target_language != "en":
                raise ProtocolError(ErrorCode.INVALID_SESSION_START, "协议 1.0 仅支持中文到英文。")
            if self.mode != "turn_stream":
                raise ProtocolError(
                    ErrorCode.INVALID_SESSION_START,
                    "协议 1.0 mode 必须是 turn_stream。",
                )
            if self.bridge_id is not None or self.session_role is not None:
                raise ProtocolError(
                    ErrorCode.INVALID_SESSION_START,
                    "协议 1.0 不接受 Meeting Bridge 字段。",
                )
        else:
            self._validate_meeting_bridge()
        if self.pipeline_provider not in {None, "livetranslate", "modular"}:
            raise ProtocolError(
                ErrorCode.INVALID_SESSION_START,
                "pipeline_provider 必须是 livetranslate 或 modular。",
            )
        if self.voice_mode not in {"standard", "clone_once"}:
            raise ProtocolError(
                ErrorCode.INVALID_SESSION_START,
                "voice_mode 必须是 standard 或 clone_once。",
            )
        if (
            self.audio.format != "pcm_s16le"
            or self.audio.sample_rate != 16000
            or self.audio.channels != 1
            or self.audio.chunk_ms <= 0
        ):
            raise ProtocolError(
                ErrorCode.INVALID_AUDIO_FORMAT,
                "音频必须是 16 kHz、单声道、16-bit little-endian PCM。",
            )

    def _validate_meeting_bridge(self) -> None:
        if self.mode != "meeting_bridge":
            raise ProtocolError(
                ErrorCode.INVALID_SESSION_START,
                "协议 1.1 mode 必须是 meeting_bridge。",
            )
        try:
            uuid.UUID(str(self.bridge_id))
        except (TypeError, ValueError) as exc:
            raise ProtocolError(
                ErrorCode.INVALID_BRIDGE_ID, "bridge_id 必须是 UUID。"
            ) from exc
        expected_pairs = {
            "local_to_remote": ("zh", "en"),
            "remote_to_local": ("en", "zh"),
        }
        if self.session_role not in expected_pairs:
            raise ProtocolError(
                ErrorCode.INVALID_SESSION_ROLE,
                "session_role 必须是 local_to_remote 或 remote_to_local。",
            )
        if (self.source_language, self.target_language) != expected_pairs[self.session_role]:
            raise ProtocolError(
                ErrorCode.INVALID_SESSION_ROLE,
                "Meeting Bridge 方向与 source/target language 不匹配。",
            )
        if self.session_role == "remote_to_local" and self.voice_mode != "standard":
            raise ProtocolError(
                ErrorCode.INVALID_SESSION_START,
                "remote_to_local 本轮只允许标准音色。",
            )

    def to_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {
            "type": "session.start",
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "mode": self.mode,
            "voice": self.voice,
            "voice_mode": self.voice_mode,
            "source_transcription_enabled": self.source_transcription_enabled,
            "audio": {
                "format": self.audio.format,
                "sample_rate": self.audio.sample_rate,
                "channels": self.audio.channels,
                "chunk_ms": self.audio.chunk_ms,
            },
            "client": {
                "platform": self.client_platform,
                "app_version": self.app_version,
            },
        }
        if self.pipeline_provider:
            message["pipeline_provider"] = self.pipeline_provider
        if self.bridge_id:
            message["bridge_id"] = self.bridge_id
        if self.session_role:
            message["session_role"] = self.session_role
        return message

    @property
    def is_meeting_bridge(self) -> bool:
        return self.protocol_version == MEETING_PROTOCOL_VERSION


def parse_control_message(payload: str) -> dict[str, Any]:
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise ProtocolError(ErrorCode.INVALID_SESSION_START, "控制消息不是有效 JSON。") from exc
    if not isinstance(data, dict) or data.get("type") not in CLIENT_MESSAGE_TYPES:
        raise ProtocolError(ErrorCode.INVALID_SESSION_START, "未知控制消息类型。")
    return data


def error_event(
    *,
    session_id: str,
    request_id: str,
    code: ErrorCode,
    message: str,
    turn_id: str | None = None,
    recoverable: bool = False,
) -> dict[str, Any]:
    return {
        "type": "error",
        "session_id": session_id,
        "turn_id": turn_id,
        "request_id": request_id,
        "code": code.value,
        "message": message,
        "recoverable": recoverable,
    }


def new_id() -> str:
    return str(uuid.uuid4())
