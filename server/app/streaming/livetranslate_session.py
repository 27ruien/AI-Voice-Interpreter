from __future__ import annotations

import asyncio
import logging
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

from ai_voice_interpreter.streaming.metrics import SessionMetrics
from ai_voice_interpreter.streaming.protocol import (
    ErrorCode,
    ProtocolError,
    SessionStart,
    new_id,
    parse_control_message,
)

from ..config import ServerConfig
from ..providers.livetranslate import (
    LiveTranslateProviderError,
    LiveTranslateSessionOptions,
    LiveTranslateUpstreamSession,
    TranscriptNormalizer,
    decode_audio_delta,
    is_source_transcription_error,
    is_voice_clone_error,
)
from .state import SessionState

logger = logging.getLogger(__name__)


class LiveTranslateStartupFailure(RuntimeError):
    def __init__(self, error: Exception) -> None:
        super().__init__(str(error))
        self.error = error


@dataclass(slots=True)
class LiveTurnContext:
    turn_id: str
    started_at: float
    item_id: str | None = None
    response_id: str | None = None
    recognized_text: str = ""
    translated_text: str = ""
    source_first_at: float = 0.0
    source_final_at: float = 0.0
    translation_first_at: float = 0.0
    translation_final_at: float = 0.0
    first_audio_at: float = 0.0
    audio_chunks: int = 0
    audio_bytes: int = 0
    audio_started: bool = False
    audio_ended: bool = False
    completed: bool = False
    usage: dict[str, Any] = field(default_factory=dict)
    last_event_id: str | None = None


class LiveTranslateGatewaySession:
    def __init__(
        self,
        websocket: WebSocket,
        config: ServerConfig,
        start_message: SessionStart,
        *,
        upstream: LiveTranslateUpstreamSession | None = None,
    ) -> None:
        self.websocket = websocket
        self.config = config
        self.start_message = start_message
        self.session_id = new_id()
        self.request_id = start_message.request_id
        self.started_at = time.monotonic()
        self.state = SessionState.CONNECTED
        self.metrics = SessionMetrics()
        self.upstream = upstream or LiveTranslateUpstreamSession(
            config,
            LiveTranslateSessionOptions(
                source_language=start_message.source_language,
                target_language=start_message.target_language,
                voice_mode=start_message.voice_mode,
                source_transcription_enabled=start_message.source_transcription_enabled,
            ),
        )
        self.source_normalizers: dict[str, TranscriptNormalizer] = {}
        self.translation_normalizers: dict[str, TranscriptNormalizer] = {}
        self.turns: dict[str, LiveTurnContext] = {}
        self.source_turns: dict[str, str] = {}
        self.response_turns: dict[str, str] = {}
        self.unbound_source_turns: list[str] = []
        self.unbound_response_turns: list[str] = []
        self.active_audio_response_id: str | None = None
        self._turn_count = 0
        self._send_lock = asyncio.Lock()
        self._closed = False
        self._output_started = False
        self._disconnect_reason = "unknown"
        self._last_event_id: str | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self._events_task: asyncio.Task[None] | None = None

    @property
    def output_started(self) -> bool:
        return self._output_started

    async def run(self) -> None:
        try:
            await self.upstream.start()
        except Exception as exc:
            await self.cleanup()
            raise LiveTranslateStartupFailure(exc) from exc

        assert self.upstream.output_spec is not None
        try:
            self.state = SessionState.LISTENING
            await self._send_json(
                {
                    "type": "session.started",
                    "session_id": self.session_id,
                    "request_id": self.request_id,
                    "protocol_version": self.config.streaming_protocol_version,
                    "pipeline_provider": "livetranslate",
                    "upstream_model": self.upstream.model,
                    "upstream_session_id": self.upstream.session_id,
                    "voice_mode": self.start_message.voice_mode,
                    "source_transcription_enabled": (
                        self.start_message.source_transcription_enabled
                        and self.config.livetranslate_enable_source_transcription
                    ),
                    "audio_output": {
                        "format": self.upstream.output_spec.format,
                        "sample_rate": self.upstream.output_spec.sample_rate,
                        "channels": self.upstream.output_spec.channels,
                        "sample_width": self.upstream.output_spec.sample_width,
                    },
                }
            )
            await self._send_json(
                {
                    "type": "provider.started",
                    "session_id": self.session_id,
                    "pipeline_provider": "livetranslate",
                    "upstream_model": self.upstream.model,
                    "upstream_session_id": self.upstream.session_id,
                }
            )
            await self._send_json(
                {
                    "type": "voice_clone.status",
                    "session_id": self.session_id,
                    "enabled": self.start_message.voice_mode == "clone_once",
                    "frequency": (
                        "once" if self.start_message.voice_mode == "clone_once" else None
                    ),
                    "status": (
                        "waiting_for_voice" if self.start_message.voice_mode == "clone_once"
                        else "disabled"
                    ),
                }
            )
            logger.info(
                "LiveTranslate session started session_id=%s client_request_id=%s "
                "upstream_session_id=%s model=%s voice_mode=%s",
                self.session_id,
                self.request_id,
                self.upstream.session_id,
                self.upstream.model,
                self.start_message.voice_mode,
            )
            receiver_task = asyncio.create_task(self._receive_client())
            events_task = asyncio.create_task(self._forward_upstream_events())
            self._receiver_task = receiver_task
            self._events_task = events_task
            done, _pending = await asyncio.wait(
                {receiver_task, events_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if events_task in done:
                await events_task
                if not receiver_task.done():
                    receiver_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await receiver_task
            else:
                await receiver_task
                self.state = SessionState.STOPPING
                await self.upstream.finish()
                try:
                    await asyncio.wait_for(
                        events_task,
                        timeout=self.config.livetranslate_session_finish_timeout_seconds,
                    )
                except TimeoutError as exc:
                    events_task.cancel()
                    with suppress(asyncio.CancelledError, Exception):
                        await events_task
                    raise ProtocolError(
                        ErrorCode.LIVETRANSLATE_FINISH_TIMEOUT,
                        "等待 LiveTranslate session.finished 超时。",
                    ) from exc
            self.state = SessionState.CLOSED
            self._disconnect_reason = "session_completed"
            await self._send_json(
                {
                    "type": "session.completed",
                    "session_id": self.session_id,
                    "turn_count": self._turn_count,
                    "duration_ms": round((time.monotonic() - self.started_at) * 1000, 1),
                    "pipeline_provider": "livetranslate",
                    "upstream_session_id": self.upstream.session_id,
                    "last_event_id": self._last_event_id,
                    "queue_peaks": {
                        "audio_input": self.upstream.audio_queue_peak,
                        "upstream_output": self.upstream.output_queue_peak,
                    },
                    "active_upstream_connections": max(
                        0, LiveTranslateUpstreamSession.active_connections - 1
                    ),
                }
            )
            await self.websocket.close(code=1000)
        except WebSocketDisconnect:
            self.state = SessionState.CLOSED
            self._disconnect_reason = "client_disconnect"
            logger.info("LiveTranslate client disconnected session_id=%s", self.session_id)
        except ProtocolError as exc:
            self.state = SessionState.FAILED
            self._disconnect_reason = exc.code.value
            await self._safe_error(exc.code.value, exc.message)
        except LiveTranslateProviderError as exc:
            self.state = SessionState.FAILED
            self._disconnect_reason = exc.code
            if is_source_transcription_error(exc):
                await self._send_source_transcription_warning(exc)
            else:
                code = "VOICE_CLONE_FAILED" if is_voice_clone_error(exc) else exc.code
                if is_voice_clone_error(exc):
                    await self._send_json(
                        {
                            "type": "voice_clone.status",
                            "session_id": self.session_id,
                            "enabled": True,
                            "frequency": "once",
                            "status": "failed",
                            "code": "VOICE_CLONE_FAILED",
                            "message": "声音复刻失败，请改用标准音色。",
                        }
                    )
                await self._safe_error(
                    code,
                    exc.message,
                    event_id=exc.event_id,
                    upstream_response_id=exc.response_id,
                )
        except Exception as exc:
            self.state = SessionState.FAILED
            self._disconnect_reason = ErrorCode.INTERNAL_ERROR.value
            logger.exception(
                "LiveTranslate gateway session failed session_id=%s type=%s",
                self.session_id,
                type(exc).__name__,
            )
            await self._safe_error(
                ErrorCode.INTERNAL_ERROR.value, "LiveTranslate 会话异常结束。"
            )
        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        current = asyncio.current_task()
        for task in (self._receiver_task, self._events_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
        for task in (self._receiver_task, self._events_task):
            if task is not None and task is not current:
                with suppress(asyncio.CancelledError, Exception):
                    await task
        await self.upstream.cancel()
        logger.info(
            "LiveTranslate session closed session_id=%s client_request_id=%s "
            "upstream_session_id=%s state=%s turns=%d duration_ms=%.1f "
            "input_seconds=%.3f audio_queue_peak=%d output_queue_peak=%d "
            "disconnect_reason=%s active_upstream=%d",
            self.session_id,
            self.request_id,
            self.upstream.session_id,
            self.state,
            self._turn_count,
            (time.monotonic() - self.started_at) * 1000,
            self.metrics.input_audio_bytes / (16000 * 2),
            self.upstream.audio_queue_peak,
            self.upstream.output_queue_peak,
            self._disconnect_reason,
            LiveTranslateUpstreamSession.active_connections,
        )

    async def _receive_client(self) -> None:
        while True:
            try:
                message = await asyncio.wait_for(
                    self.websocket.receive(),
                    timeout=self.config.streaming_heartbeat_timeout_seconds,
                )
            except TimeoutError as exc:
                raise ProtocolError(
                    ErrorCode.HEARTBEAT_TIMEOUT, "客户端心跳超时。"
                ) from exc
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            pcm = message.get("bytes")
            if pcm is not None:
                if len(pcm) > self.config.streaming_max_frame_bytes:
                    raise ProtocolError(
                        ErrorCode.AUDIO_FRAME_TOO_LARGE, "音频 Frame 超过服务器限制。"
                    )
                if not pcm:
                    continue
                self.metrics.input_audio_bytes += len(pcm)
                self.metrics.websocket_received_bytes += len(pcm)
                try:
                    await self.upstream.send_audio(pcm)
                except LiveTranslateProviderError as exc:
                    if exc.code == "AUDIO_QUEUE_FULL":
                        raise ProtocolError(
                            ErrorCode.SERVER_BACKPRESSURE,
                            "LiveTranslate 音频队列已满，停止当前会话。",
                        ) from exc
                    raise
                continue
            text = message.get("text")
            if text is None:
                continue
            self.metrics.websocket_received_bytes += len(text.encode())
            control = parse_control_message(text)
            if control["type"] == "session.stop":
                self._disconnect_reason = "client_stop"
                return
            if control["type"] == "ping":
                await self._send_json(
                    {
                        "type": "pong",
                        "session_id": self.session_id,
                        "timestamp_ms": control.get("timestamp_ms"),
                    }
                )
            elif control["type"] == "session.start":
                raise ProtocolError(
                    ErrorCode.INVALID_SESSION_START, "同一连接不能重复 session.start。"
                )

    async def _forward_upstream_events(self) -> None:
        async for event in self.upstream.events():
            event_type = str(event.get("type", ""))
            self._last_event_id = _optional_text(event.get("event_id")) or self._last_event_id
            if event_type == "error":
                error = LiveTranslateProviderError.from_event(event)
                if is_source_transcription_error(error):
                    await self._send_source_transcription_warning(error)
                    continue
                raise error
            if event_type == "conversation.item.input_audio_transcription.text":
                await self._handle_source_partial(event)
            elif event_type == "conversation.item.input_audio_transcription.completed":
                await self._handle_source_final(event)
            elif event_type == "response.created":
                await self._handle_response_created(event)
            elif event_type in {"response.audio_transcript.text", "response.text.text"}:
                await self._handle_translation_partial(event)
            elif event_type in {"response.audio_transcript.done", "response.text.done"}:
                await self._handle_translation_final(event)
            elif event_type == "response.audio.delta":
                await self._handle_audio_delta(event)
            elif event_type == "response.audio.done":
                await self._handle_audio_done(event)
            elif event_type == "response.done":
                await self._handle_response_done(event)
            elif event_type == "session.finished":
                return

    async def _handle_source_partial(self, event: dict[str, Any]) -> None:
        item_id = _required_id(event, "item_id")
        normalizer = self.source_normalizers.setdefault(item_id, TranscriptNormalizer())
        display = normalizer.update(event.get("text"), event.get("stash"))
        if display is None:
            return
        context = self._source_context(item_id)
        context.last_event_id = _optional_text(event.get("event_id"))
        if not context.source_first_at:
            context.source_first_at = time.monotonic()
            await self._send_json(
                {
                    "type": "vad.speech_start",
                    "session_id": self.session_id,
                    "turn_id": context.turn_id,
                    "speech_ms": 0,
                    "detector": "livetranslate",
                }
            )
        await self._send_json(
            {
                "type": "asr.partial",
                "session_id": self.session_id,
                "turn_id": context.turn_id,
                "text": display,
                "confirmed_text": normalizer.confirmed,
                "predicted_text": normalizer.stash,
                "is_final": False,
                "upstream_item_id": item_id,
                "event_id": context.last_event_id,
            }
        )

    async def _handle_source_final(self, event: dict[str, Any]) -> None:
        item_id = _required_id(event, "item_id")
        normalizer = self.source_normalizers.setdefault(item_id, TranscriptNormalizer())
        final = normalizer.complete(event.get("transcript"))
        if final is None:
            return
        context = self._source_context(item_id)
        context.recognized_text = final
        context.source_final_at = time.monotonic()
        context.last_event_id = _optional_text(event.get("event_id"))
        await self._send_json(
            {
                "type": "vad.speech_end",
                "session_id": self.session_id,
                "turn_id": context.turn_id,
                "speech_ms": 0,
                "silence_ms": 0,
                "forced": False,
                "detector": "livetranslate",
            }
        )
        await self._send_json(
            {
                "type": "asr.final",
                "session_id": self.session_id,
                "turn_id": context.turn_id,
                "text": final,
                "is_final": True,
                "upstream_item_id": item_id,
                "event_id": context.last_event_id,
            }
        )
        logger.info(
            "LiveTranslate source final session_id=%s turn_id=%s item_id=%s text_length=%d",
            self.session_id,
            context.turn_id,
            item_id,
            len(final),
        )

    async def _handle_response_created(self, event: dict[str, Any]) -> None:
        response = event.get("response")
        if not isinstance(response, dict):
            raise LiveTranslateProviderError(
                "INVALID_RESPONSE", "response.created 缺少 Response。"
            )
        response_id = _required_id(response, "id")
        context = self._response_context(response_id)
        context.last_event_id = _optional_text(event.get("event_id"))

    async def _handle_translation_partial(self, event: dict[str, Any]) -> None:
        response_id = _required_id(event, "response_id")
        context = self._response_context(response_id)
        normalizer = self.translation_normalizers.setdefault(
            response_id, TranscriptNormalizer()
        )
        display = normalizer.update(event.get("text"), event.get("stash"))
        if display is None:
            return
        if not context.translation_first_at:
            context.translation_first_at = time.monotonic()
        context.translated_text = display
        context.last_event_id = _optional_text(event.get("event_id"))
        await self._send_json(
            {
                "type": "translation.partial",
                "session_id": self.session_id,
                "turn_id": context.turn_id,
                "text": display,
                "confirmed_text": normalizer.confirmed,
                "predicted_text": normalizer.stash,
                "upstream_response_id": response_id,
                "upstream_item_id": _optional_text(event.get("item_id")),
                "event_id": context.last_event_id,
            }
        )

    async def _handle_translation_final(self, event: dict[str, Any]) -> None:
        response_id = _required_id(event, "response_id")
        context = self._response_context(response_id)
        normalizer = self.translation_normalizers.setdefault(
            response_id, TranscriptNormalizer()
        )
        final_field = (
            event.get("transcript")
            if event.get("type") == "response.audio_transcript.done"
            else event.get("text")
        )
        final = normalizer.complete(final_field)
        if final is None:
            return
        context.translated_text = final
        context.translation_final_at = time.monotonic()
        context.last_event_id = _optional_text(event.get("event_id"))
        await self._send_json(
            {
                "type": "translation.final",
                "session_id": self.session_id,
                "turn_id": context.turn_id,
                "text": final,
                "upstream_response_id": response_id,
                "upstream_item_id": _optional_text(event.get("item_id")),
                "event_id": context.last_event_id,
            }
        )
        logger.info(
            "LiveTranslate translation final session_id=%s turn_id=%s response_id=%s "
            "text_length=%d",
            self.session_id,
            context.turn_id,
            response_id,
            len(final),
        )

    async def _handle_audio_delta(self, event: dict[str, Any]) -> None:
        response_id = _required_id(event, "response_id")
        context = self._response_context(response_id)
        if self.active_audio_response_id not in {None, response_id}:
            raise LiveTranslateProviderError(
                "INTERLEAVED_AUDIO", "两个 LiveTranslate Response 的音频发生交叉。"
            )
        pcm = decode_audio_delta(event.get("delta"))
        if not context.audio_started:
            assert self.upstream.output_spec is not None
            context.audio_started = True
            context.first_audio_at = time.monotonic()
            self.active_audio_response_id = response_id
            self._output_started = True
            await self._send_json(
                {
                    "type": "tts.audio.start",
                    "session_id": self.session_id,
                    "turn_id": context.turn_id,
                    "format": self.upstream.output_spec.format,
                    "sample_rate": self.upstream.output_spec.sample_rate,
                    "channels": self.upstream.output_spec.channels,
                    "sample_width": self.upstream.output_spec.sample_width,
                    "upstream_response_id": response_id,
                    "event_id": _optional_text(event.get("event_id")),
                }
            )
            if self.start_message.voice_mode == "clone_once":
                await self._send_json(
                    {
                        "type": "voice_clone.status",
                        "session_id": self.session_id,
                        "enabled": True,
                        "frequency": "once",
                        "status": "audio_available",
                    }
                )
        context.audio_chunks += 1
        context.audio_bytes += len(pcm)
        await self._send_bytes(pcm)

    async def _handle_audio_done(self, event: dict[str, Any]) -> None:
        response_id = _required_id(event, "response_id")
        context = self._response_context(response_id)
        if context.audio_ended:
            return
        if not context.audio_started:
            raise LiveTranslateProviderError(
                "INVALID_AUDIO", "LiveTranslate 音频完成前未返回任何音频块。"
            )
        context.audio_ended = True
        self.active_audio_response_id = None
        await self._send_json(
            {
                "type": "tts.audio.end",
                "session_id": self.session_id,
                "turn_id": context.turn_id,
                "audio_bytes": context.audio_bytes,
                "audio_chunks": context.audio_chunks,
                "upstream_response_id": response_id,
                "event_id": _optional_text(event.get("event_id")),
            }
        )

    async def _handle_response_done(self, event: dict[str, Any]) -> None:
        response = event.get("response")
        if not isinstance(response, dict):
            raise LiveTranslateProviderError(
                "INVALID_RESPONSE", "response.done 缺少 Response。"
            )
        response_id = _required_id(response, "id")
        context = self._response_context(response_id)
        status = str(response.get("status", ""))
        if status not in {"completed", ""}:
            raise LiveTranslateProviderError(
                "RESPONSE_NOT_COMPLETED",
                f"LiveTranslate Response 状态为 {status}。",
                response_id=response_id,
                event_id=_optional_text(event.get("event_id")),
            )
        if not context.translated_text:
            final = _translation_from_response(response)
            normalizer = self.translation_normalizers.setdefault(
                response_id, TranscriptNormalizer()
            )
            completed = normalizer.complete(final)
            if completed:
                context.translated_text = completed
                context.translation_final_at = time.monotonic()
                await self._send_json(
                    {
                        "type": "translation.final",
                        "session_id": self.session_id,
                        "turn_id": context.turn_id,
                        "text": completed,
                        "upstream_response_id": response_id,
                        "event_id": _optional_text(event.get("event_id")),
                    }
                )
        usage = response.get("usage")
        context.usage = usage if isinstance(usage, dict) else {}
        await self._send_json(
            {
                "type": "usage.updated",
                "session_id": self.session_id,
                "turn_id": context.turn_id,
                "upstream_response_id": response_id,
                "usage": context.usage,
            }
        )
        if context.completed:
            return
        if "audio" in self.config.livetranslate_output_modalities and not context.audio_ended:
            raise LiveTranslateProviderError(
                "INVALID_AUDIO",
                "LiveTranslate Response 完成但没有完整音频。",
                response_id=response_id,
            )
        context.completed = True
        self._turn_count += 1
        now = time.monotonic()
        metrics = {
            "asr_first_partial_ms": _elapsed(context.started_at, context.source_first_at),
            "turn_finalize_ms": 0.0,
            "translation_first_token_ms": _elapsed(
                context.source_final_at or context.started_at,
                context.translation_first_at,
            ),
            "translation_final_ms": _elapsed(
                context.source_final_at or context.started_at,
                context.translation_final_at,
            ),
            "tts_first_audio_ms": _elapsed(
                context.translation_first_at or context.started_at,
                context.first_audio_at,
            ),
            "server_time_to_first_audio_ms": _elapsed(
                context.source_final_at or context.started_at,
                context.first_audio_at,
            ),
            "client_first_playback_ms": 0.0,
            "end_to_end_ttfa_ms": 0.0,
            "turn_total_ms": max(0.0, (now - context.started_at) * 1000),
        }
        await self._send_json(
            {
                "type": "turn.completed",
                "session_id": self.session_id,
                "turn_id": context.turn_id,
                "recognized_text": context.recognized_text,
                "translated_text": context.translated_text,
                "metrics": metrics,
                "pipeline_provider": "livetranslate",
                "upstream_session_id": self.upstream.session_id,
                "upstream_response_id": response_id,
                "event_id": _optional_text(event.get("event_id")),
                "usage": context.usage,
                "audio_chunks": context.audio_chunks,
                "audio_bytes": context.audio_bytes,
            }
        )
        logger.info(
            "LiveTranslate turn completed session_id=%s turn_id=%s response_id=%s "
            "source_length=%d translation_length=%d audio_chunks=%d audio_bytes=%d "
            "total_ms=%.1f",
            self.session_id,
            context.turn_id,
            response_id,
            len(context.recognized_text),
            len(context.translated_text),
            context.audio_chunks,
            context.audio_bytes,
            metrics["turn_total_ms"],
        )

    def _source_context(self, item_id: str) -> LiveTurnContext:
        turn_id = self.source_turns.get(item_id)
        if turn_id is None:
            turn_id = next(
                (
                    candidate
                    for candidate in self.unbound_response_turns
                    if self.turns[candidate].item_id is None
                ),
                new_id(),
            )
            self.source_turns[item_id] = turn_id
            context = self.turns.get(turn_id)
            if context is None:
                context = LiveTurnContext(
                    turn_id=turn_id,
                    started_at=time.monotonic(),
                    item_id=item_id,
                )
                self.turns[turn_id] = context
                self.unbound_source_turns.append(turn_id)
            else:
                context.item_id = item_id
        return self.turns[turn_id]

    def _response_context(self, response_id: str) -> LiveTurnContext:
        turn_id = self.response_turns.get(response_id)
        if turn_id is None:
            turn_id = next(
                (
                    candidate
                    for candidate in self.unbound_source_turns
                    if self.turns[candidate].response_id is None
                ),
                new_id(),
            )
            context = self.turns.get(turn_id)
            if context is None:
                context = LiveTurnContext(turn_id=turn_id, started_at=time.monotonic())
                self.turns[turn_id] = context
                self.unbound_response_turns.append(turn_id)
            context.response_id = response_id
            self.response_turns[response_id] = turn_id
        return self.turns[turn_id]

    async def _send_source_transcription_warning(
        self, error: LiveTranslateProviderError
    ) -> None:
        await self._send_json(
            {
                "type": "source_transcription.unavailable",
                "session_id": self.session_id,
                "code": ErrorCode.SOURCE_TRANSCRIPTION_UNAVAILABLE.value,
                "message": "源语言字幕暂不可用，翻译与音频将继续。",
                "upstream_code": error.code,
                "event_id": error.event_id,
            }
        )
        await self._send_json(
            {
                "type": "warning",
                "session_id": self.session_id,
                "code": ErrorCode.SOURCE_TRANSCRIPTION_UNAVAILABLE.value,
                "message": "源语言字幕暂不可用。",
            }
        )

    async def _send_json(self, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.websocket.send_json(payload)
            self.metrics.websocket_sent_bytes += len(str(payload).encode())

    async def _send_bytes(self, payload: bytes) -> None:
        async with self._send_lock:
            await self.websocket.send_bytes(payload)
            self.metrics.websocket_sent_bytes += len(payload)

    async def _safe_error(
        self,
        code: str,
        message: str,
        *,
        event_id: str | None = None,
        upstream_response_id: str | None = None,
    ) -> None:
        logger.warning(
            "LiveTranslate session error session_id=%s client_request_id=%s code=%s",
            self.session_id,
            self.request_id,
            code,
        )
        try:
            await self._send_json(
                {
                    "type": "error",
                    "session_id": self.session_id,
                    "request_id": self.request_id,
                    "code": code,
                    "message": message,
                    "recoverable": not self._output_started,
                    "pipeline_provider": "livetranslate",
                    "upstream_session_id": self.upstream.session_id,
                    "upstream_response_id": upstream_response_id,
                    "event_id": event_id,
                }
            )
            await self.websocket.close(code=1011)
        except Exception:
            logger.debug("Unable to send LiveTranslate error to disconnected client")


def _required_id(payload: dict[str, Any], key: str) -> str:
    value = _optional_text(payload.get(key))
    if not value:
        raise LiveTranslateProviderError(
            "INVALID_UPSTREAM_EVENT", f"LiveTranslate 事件缺少 {key}。"
        )
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _translation_from_response(response: dict[str, Any]) -> str:
    output = response.get("output")
    if not isinstance(output, list):
        return ""
    for item in output:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if not isinstance(part, dict):
                continue
            value = part.get("transcript") or part.get("text")
            if value:
                return str(value)
    return ""


def _elapsed(start: float, end: float) -> float:
    if not start or not end:
        return 0.0
    return max(0.0, (end - start) * 1000)
