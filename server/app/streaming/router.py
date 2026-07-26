from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from fastapi import WebSocket

from ai_voice_interpreter.streaming.protocol import (
    ErrorCode,
    ProtocolError,
    SessionStart,
    error_event,
    new_id,
)

from ..config import ServerConfig
from ..providers.livetranslate import (
    LiveTranslateSessionOptions,
    LiveTranslateUpstreamSession,
)
from .livetranslate_session import (
    LiveTranslateGatewaySession,
    LiveTranslateStartupFailure,
)
from .session import StreamDependencies, StreamingSession

logger = logging.getLogger(__name__)


class StreamingPipelineSession(Protocol):
    async def run(self) -> None: ...
    async def cleanup(self) -> None: ...


class StreamingPipelineRouter:
    def __init__(
        self,
        config: ServerConfig,
        modular_dependencies: StreamDependencies,
        *,
        upstream_factory: Callable[
            [ServerConfig, LiveTranslateSessionOptions], LiveTranslateUpstreamSession
        ] = LiveTranslateUpstreamSession,
    ) -> None:
        self.config = config
        self.modular_dependencies = modular_dependencies
        self.upstream_factory = upstream_factory

    def resolve_provider(self, start: SessionStart) -> str:
        configuration_error = self.config.stream_provider_configuration_error
        if configuration_error:
            raise ProtocolError(
                ErrorCode.PIPELINE_CONFIGURATION_INVALID, configuration_error
            )
        if start.pipeline_provider:
            if not self.config.allow_stream_pipeline_override:
                raise ProtocolError(
                    ErrorCode.PIPELINE_OVERRIDE_DISABLED,
                    "服务器未开放 Streaming Pipeline 覆盖。",
                )
            return start.pipeline_provider
        return self.config.stream_pipeline_provider

    def create_session(
        self,
        provider_name: str,
        websocket: WebSocket,
        start: SessionStart,
        *,
        fallback_from: str | None = None,
    ) -> StreamingPipelineSession:
        if provider_name == "modular":
            return StreamingSession(
                websocket,
                self.config,
                self.modular_dependencies,
                start_message=start,
                fallback_from=fallback_from,
            )
        if provider_name == "livetranslate":
            options = LiveTranslateSessionOptions(
                source_language=start.source_language,
                target_language=start.target_language,
                voice_mode=start.voice_mode,
                source_transcription_enabled=start.source_transcription_enabled,
            )
            return LiveTranslateGatewaySession(
                websocket,
                self.config,
                start,
                upstream=self.upstream_factory(self.config, options),
            )
        raise ProtocolError(
            ErrorCode.PIPELINE_CONFIGURATION_INVALID,
            f"未知 Streaming Pipeline：{provider_name}。",
        )

    @staticmethod
    def should_automatically_fallback(
        *, output_started: bool, automatic_switches: int
    ) -> bool:
        return not output_started and automatic_switches == 0


class RoutedStreamingSession:
    def __init__(
        self,
        websocket: WebSocket,
        config: ServerConfig,
        router: StreamingPipelineRouter,
    ) -> None:
        self.websocket = websocket
        self.config = config
        self.router = router
        self.session_id = new_id()
        self.request_id = new_id()
        self._active: StreamingPipelineSession | None = None
        self._closed = False
        self._automatic_switches = 0

    async def run(self) -> None:
        try:
            try:
                first = await asyncio.wait_for(
                    self.websocket.receive(),
                    timeout=self.config.streaming_heartbeat_timeout_seconds,
                )
            except TimeoutError as exc:
                raise ProtocolError(
                    ErrorCode.HEARTBEAT_TIMEOUT, "等待 session.start 超时。"
                ) from exc
            if first.get("text") is None:
                raise ProtocolError(
                    ErrorCode.INVALID_SESSION_START,
                    "发送音频前必须先发送 session.start。",
                )
            start = SessionStart.parse(first["text"])
            self.request_id = start.request_id
            provider = self.router.resolve_provider(start)
            self._active = self.router.create_session(provider, self.websocket, start)
            try:
                await self._active.run()
            except LiveTranslateStartupFailure as startup:
                if (
                    provider == "livetranslate"
                    and self.config.stream_pipeline_fallback_provider == "modular"
                    and self.router.should_automatically_fallback(
                        output_started=False,
                        automatic_switches=self._automatic_switches,
                    )
                ):
                    self._automatic_switches += 1
                    error_code = getattr(
                        startup.error, "code", type(startup.error).__name__
                    )
                    error_event_id = getattr(startup.error, "event_id", None)
                    logger.warning(
                        "LiveTranslate startup failed; switching once to modular "
                        "client_request_id=%s type=%s code=%s event_id=%s",
                        self.request_id,
                        type(startup.error).__name__,
                        error_code,
                        error_event_id or "unavailable",
                    )
                    self._active = self.router.create_session(
                        "modular",
                        self.websocket,
                        start,
                        fallback_from="livetranslate",
                    )
                    await self._active.run()
                else:
                    raise ProtocolError(
                        ErrorCode.LIVETRANSLATE_CONNECTION_FAILED,
                        _safe_startup_message(startup.error),
                    ) from startup
        except ProtocolError as exc:
            await self._safe_error(exc.code, exc.message)

    async def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._active is not None:
            await self._active.cleanup()

    async def _safe_error(self, code: ErrorCode, message: str) -> None:
        try:
            await self.websocket.send_json(
                error_event(
                    session_id=self.session_id,
                    request_id=self.request_id,
                    code=code,
                    message=message,
                    recoverable=code
                    in {
                        ErrorCode.LIVETRANSLATE_CONNECTION_FAILED,
                        ErrorCode.FALLBACK_REQUIRED,
                    },
                )
            )
            await self.websocket.close(code=1011)
        except Exception:
            logger.debug("Unable to send routed streaming error")


def _safe_startup_message(error: Exception) -> str:
    code = getattr(error, "code", type(error).__name__)
    message = getattr(error, "message", "LiveTranslate 连接失败。")
    return f"{message} code={code}"
