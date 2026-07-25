from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from ai_voice_interpreter.streaming.metrics import QueueMetrics, SessionMetrics, TurnMetrics
from ai_voice_interpreter.streaming.protocol import (
    ErrorCode,
    ProtocolError,
    SessionStart,
    error_event,
    new_id,
    parse_control_message,
)
from ai_voice_interpreter.streaming.segmenter import TTSTextSegmenter

from ..config import ServerConfig
from ..providers.realtime_asr import DashScopeRealtimeASRSession
from ..providers.streaming_interfaces import (
    RealtimeASRSession,
    StreamingTranslator,
    StreamingTTSSession,
    TranslationStreamEvent,
)
from ..providers.streaming_translation import DashScopeStreamingTranslator
from ..providers.streaming_tts import DashScopeStreamingTTSSession
from .state import SessionState, TurnState
from .vad import TurnVAD, VADEvent, VADEventType

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StreamDependencies:
    asr_factory: Callable[[], RealtimeASRSession]
    translator_factory: Callable[[], StreamingTranslator]
    tts_factory: Callable[[], StreamingTTSSession]
    vad_factory: Callable[[], TurnVAD]

    @classmethod
    def real(cls, config: ServerConfig) -> StreamDependencies:
        provider_config = config.provider_config(asr_model=config.stream_asr_model)
        return cls(
            asr_factory=lambda: DashScopeRealtimeASRSession(
                provider_config, config.stream_audio_queue_max_chunks
            ),
            translator_factory=lambda: DashScopeStreamingTranslator(provider_config),
            tts_factory=lambda: DashScopeStreamingTTSSession(
                provider_config, config.stream_tts_audio_queue_max_chunks
            ),
            vad_factory=lambda: TurnVAD(
                sample_rate=config.stream_audio_sample_rate,
                frame_ms=config.vad_frame_ms,
                aggressiveness=config.vad_aggressiveness,
                min_speech_ms=config.vad_min_speech_ms,
                silence_ms=config.vad_silence_ms,
                pre_roll_ms=config.vad_pre_roll_ms,
                max_turn_ms=config.vad_max_turn_ms,
            ),
        )


class StreamingConnectionRegistry:
    def __init__(self, total_limit: int, per_token_limit: int) -> None:
        self.total_limit = total_limit
        self.per_token_limit = per_token_limit
        self._total = 0
        self._by_token: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, token: str) -> bool:
        token_key = _token_fingerprint(token)
        async with self._lock:
            if self._total >= self.total_limit:
                return False
            if self._by_token.get(token_key, 0) >= self.per_token_limit:
                return False
            self._total += 1
            self._by_token[token_key] = self._by_token.get(token_key, 0) + 1
            return True

    async def release(self, token: str) -> None:
        token_key = _token_fingerprint(token)
        async with self._lock:
            self._total = max(0, self._total - 1)
            count = self._by_token.get(token_key, 0) - 1
            if count <= 0:
                self._by_token.pop(token_key, None)
            else:
                self._by_token[token_key] = count

    @property
    def active(self) -> int:
        return self._total


@dataclass(slots=True)
class TurnContext:
    turn_id: str
    started_at: float
    first_audio_at: float
    speech_end_at: float = 0.0
    asr_final_at: float = 0.0
    translation_started_at: float = 0.0
    tts_text_started_at: float = 0.0
    first_audio_sent_at: float = 0.0
    recognized_text: str = ""
    translated_text: str = ""
    asr_request_id: str | None = None
    translation_request_id: str | None = None
    tts_request_id: str | None = None
    state: TurnState = TurnState.SPEECH_ACTIVE
    metrics: TurnMetrics = field(default_factory=TurnMetrics)


@dataclass(frozen=True, slots=True)
class StableTurn:
    context: TurnContext


class StreamingSession:
    def __init__(
        self,
        websocket: WebSocket,
        config: ServerConfig,
        dependencies: StreamDependencies,
    ) -> None:
        self.websocket = websocket
        self.config = config
        self.dependencies = dependencies
        self.session_id = new_id()
        self.request_id = new_id()
        self.state = SessionState.CONNECTED
        self.started_at = time.monotonic()
        self.last_client_message_at = self.started_at
        self.start_message: SessionStart | None = None
        self.metrics = SessionMetrics()
        self.input_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            config.stream_audio_queue_max_chunks
        )
        self.turn_queue: asyncio.Queue[StableTurn | None] = asyncio.Queue(
            config.stream_turn_queue_max
        )
        self.input_queue_metrics = QueueMetrics()
        self.turn_queue_metrics = QueueMetrics()
        self.tts_text_queue_metrics = QueueMetrics()
        self._send_lock = asyncio.Lock()
        self._active_asr: RealtimeASRSession | None = None
        self._active_tts: StreamingTTSSession | None = None
        self._active_turn: TurnContext | None = None
        self._asr_event_task: asyncio.Task[tuple[str, str | None]] | None = None
        self._turn_count = 0
        self._closed = False
        self._disconnect_reason = "unknown"

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
            self.start_message = SessionStart.parse(first["text"])
            if self.start_message.audio.chunk_ms != self.config.stream_audio_chunk_ms:
                raise ProtocolError(
                    ErrorCode.INVALID_AUDIO_FORMAT,
                    f"audio.chunk_ms 必须是 {self.config.stream_audio_chunk_ms}。",
                )
            self.request_id = self.start_message.request_id
            self.state = SessionState.LISTENING
            logger.info(
                "Streaming session started session_id=%s client_request_id=%s chunk_ms=%d",
                self.session_id,
                self.request_id,
                self.start_message.audio.chunk_ms,
            )
            await self._send_json(
                {
                    "type": "session.started",
                    "session_id": self.session_id,
                    "request_id": self.request_id,
                    "protocol_version": self.config.streaming_protocol_version,
                    "audio_output": {
                        "format": "pcm_s16le",
                        "sample_rate": 24000,
                        "channels": 1,
                        "sample_width": 2,
                    },
                }
            )
            async with asyncio.TaskGroup() as tasks:
                receiver = tasks.create_task(self._receive_loop())
                audio = tasks.create_task(self._audio_loop())
                turns = tasks.create_task(self._turn_loop())
                await receiver
                await self.input_queue.put(None)
                await audio
                await self.turn_queue.put(None)
                await turns
            await self._send_json(
                {
                    "type": "session.completed",
                    "session_id": self.session_id,
                    "turn_count": self._turn_count,
                    "duration_ms": round((time.monotonic() - self.started_at) * 1000, 1),
                    "queue_peaks": {
                        "audio_input": self.input_queue_metrics.peak,
                        "turn": self.turn_queue_metrics.peak,
                        "tts_text": self.tts_text_queue_metrics.peak,
                    },
                }
            )
            self.state = SessionState.CLOSED
            if self._disconnect_reason == "unknown":
                self._disconnect_reason = "session_completed"
            await self.websocket.close(code=1000)
        except WebSocketDisconnect:
            self.state = SessionState.CLOSED
            self._disconnect_reason = "client_disconnect"
            logger.info("Streaming client disconnected session_id=%s", self.session_id)
        except ProtocolError as exc:
            self.state = SessionState.FAILED
            self._disconnect_reason = exc.code.value
            await self._safe_error(exc.code, exc.message)
        except BaseExceptionGroup as group:
            protocol_error = _find_protocol_error(group)
            if protocol_error is not None:
                self.state = SessionState.FAILED
                self._disconnect_reason = protocol_error.code.value
                await self._safe_error(protocol_error.code, protocol_error.message)
            elif _group_contains(group, WebSocketDisconnect):
                self.state = SessionState.CLOSED
                self._disconnect_reason = "client_disconnect"
                logger.info("Streaming client disconnected session_id=%s", self.session_id)
            else:
                self.state = SessionState.FAILED
                self._disconnect_reason = ErrorCode.INTERNAL_ERROR.value
                logger.exception(
                    "Streaming session failed session_id=%s type=%s",
                    self.session_id,
                    type(group).__name__,
                )
                await self._safe_error(ErrorCode.INTERNAL_ERROR, "流式会话异常结束。")
        except Exception as exc:
            self.state = SessionState.FAILED
            self._disconnect_reason = ErrorCode.INTERNAL_ERROR.value
            logger.exception(
                "Streaming session failed session_id=%s type=%s",
                self.session_id,
                type(exc).__name__,
            )
            await self._safe_error(ErrorCode.INTERNAL_ERROR, "流式会话异常结束。")
        finally:
            await self.cleanup()

    async def cleanup(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._active_asr is not None:
            await self._active_asr.cancel()
        if self._active_tts is not None:
            await self._active_tts.cancel()
        for task in (self._asr_event_task,):
            if task is not None and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await task
        self.metrics.queue_peaks = {
            "audio_input": self.input_queue_metrics.peak,
            "turn": self.turn_queue_metrics.peak,
            "tts_text": self.tts_text_queue_metrics.peak,
        }
        logger.info(
            "Streaming session closed session_id=%s client_request_id=%s state=%s turns=%d "
            "duration_ms=%.1f input_seconds=%.3f ws_received_bytes=%d ws_sent_bytes=%d "
            "provider_calls=%s fallback_count=%d queue_peaks=%s disconnect_reason=%s",
            self.session_id,
            self.request_id,
            self.state,
            self._turn_count,
            (time.monotonic() - self.started_at) * 1000,
            self.metrics.input_audio_bytes / (16000 * 2),
            self.metrics.websocket_received_bytes,
            self.metrics.websocket_sent_bytes,
            self.metrics.provider_calls,
            self.metrics.fallback_count,
            self.metrics.queue_peaks,
            self._disconnect_reason,
        )

    async def _receive_loop(self) -> None:
        while True:
            try:
                message = await asyncio.wait_for(
                    self.websocket.receive(),
                    timeout=self.config.streaming_heartbeat_timeout_seconds,
                )
            except TimeoutError as exc:
                raise ProtocolError(ErrorCode.HEARTBEAT_TIMEOUT, "客户端心跳超时。") from exc
            self.last_client_message_at = time.monotonic()
            if message.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(message.get("code", 1000))
            data = message.get("bytes")
            if data is not None:
                self.metrics.websocket_received_bytes += len(data)
                if len(data) > self.config.streaming_max_frame_bytes:
                    raise ProtocolError(
                        ErrorCode.AUDIO_FRAME_TOO_LARGE, "音频 Frame 超过服务器限制。"
                    )
                if not data:
                    continue
                try:
                    self.input_queue.put_nowait(data)
                except asyncio.QueueFull as exc:
                    raise ProtocolError(
                        ErrorCode.SERVER_BACKPRESSURE,
                        "服务器音频输入队列已满，建议切换 HTTP 按句模式。",
                    ) from exc
                self.metrics.input_audio_bytes += len(data)
                self.input_queue_metrics.observe(
                    self.input_queue.qsize(), self.input_queue.maxsize
                )
                if self.input_queue.qsize() / self.input_queue.maxsize >= 0.8:
                    logger.warning(
                        "Audio input queue pressure session_id=%s depth=%d max=%d",
                        self.session_id,
                        self.input_queue.qsize(),
                        self.input_queue.maxsize,
                    )
                continue
            text = message.get("text")
            if text is None:
                continue
            self.metrics.websocket_received_bytes += len(text.encode())
            control = parse_control_message(text)
            if control["type"] == "session.stop":
                self.state = SessionState.STOPPING
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

    async def _audio_loop(self) -> None:
        vad = self.dependencies.vad_factory()
        while True:
            chunk = await self.input_queue.get()
            self.input_queue_metrics.observe(self.input_queue.qsize(), self.input_queue.maxsize)
            if chunk is None:
                for event in vad.flush():
                    await self._handle_vad_event(event)
                return
            for event in vad.feed(chunk):
                await self._handle_vad_event(event)

    async def _handle_vad_event(self, event: VADEvent) -> None:
        now = time.monotonic()
        if event.type == VADEventType.SPEECH_START:
            if self._active_turn is not None:
                raise ProtocolError(ErrorCode.INTERNAL_ERROR, "Turn 状态发生冲突。")
            first_audio_at = now - event.speech_ms / 1000
            context = TurnContext(new_id(), now, first_audio_at)
            self._active_turn = context
            self._active_asr = self.dependencies.asr_factory()
            self.metrics.provider_calls["asr"] += 1
            logger.info(
                "Turn speech started session_id=%s turn_id=%s speech_ms=%d",
                self.session_id,
                context.turn_id,
                event.speech_ms,
            )
            try:
                await self._active_asr.start()
            except Exception as exc:
                raise ProtocolError(ErrorCode.ASR_CONNECTION_FAILED, str(exc)) from exc
            self._asr_event_task = asyncio.create_task(
                self._forward_asr_events(context, self._active_asr)
            )
            await self._send_json(
                {
                    "type": "vad.speech_start",
                    "session_id": self.session_id,
                    "turn_id": context.turn_id,
                    "speech_ms": event.speech_ms,
                }
            )
            await self._send_asr_audio(event.audio)
        elif event.type == VADEventType.SPEECH_AUDIO:
            if self._active_asr is not None:
                await self._send_asr_audio(event.audio)
        elif event.type == VADEventType.SPEECH_END and self._active_turn is not None:
            context = self._active_turn
            context.state = TurnState.FINALIZING_ASR
            context.speech_end_at = now - event.silence_ms / 1000
            logger.info(
                "Turn speech ended session_id=%s turn_id=%s speech_ms=%d silence_ms=%d "
                "forced=%s",
                self.session_id,
                context.turn_id,
                event.speech_ms,
                event.silence_ms,
                event.forced,
            )
            await self._send_json(
                {
                    "type": "vad.speech_end",
                    "session_id": self.session_id,
                    "turn_id": context.turn_id,
                    "speech_ms": event.speech_ms,
                    "silence_ms": event.silence_ms,
                    "forced": event.forced,
                }
            )
            assert self._active_asr is not None and self._asr_event_task is not None
            try:
                await self._active_asr.finish()
            except Exception as exc:
                raise ProtocolError(ErrorCode.ASR_STREAM_FAILED, str(exc)) from exc
            recognized, request_id = await self._asr_event_task
            context.asr_final_at = time.monotonic()
            context.metrics.turn_finalize_ms = max(
                0.0, (context.asr_final_at - context.speech_end_at) * 1000
            )
            context.recognized_text = recognized
            context.asr_request_id = request_id
            logger.info(
                "ASR final session_id=%s turn_id=%s request_id=%s text_length=%d "
                "finalize_ms=%.1f empty=%s",
                self.session_id,
                context.turn_id,
                request_id,
                len(recognized),
                context.metrics.turn_finalize_ms,
                not bool(recognized.strip()),
            )
            self._active_asr = None
            self._asr_event_task = None
            self._active_turn = None
            if not recognized.strip():
                await self._send_json(
                    {
                        "type": "warning",
                        "session_id": self.session_id,
                        "turn_id": context.turn_id,
                        "code": ErrorCode.ASR_EMPTY_RESULT.value,
                        "message": "本 Turn 未识别到有效语音，已跳过翻译和播放。",
                    }
                )
                return
            await self._send_json(
                {
                    "type": "asr.final",
                    "session_id": self.session_id,
                    "turn_id": context.turn_id,
                    "text": recognized,
                    "sequence": 1,
                    "is_final": True,
                    "provider_request_id": request_id,
                }
            )
            try:
                self.turn_queue.put_nowait(StableTurn(context))
            except asyncio.QueueFull as exc:
                raise ProtocolError(
                    ErrorCode.SERVER_BACKPRESSURE, "Turn 队列已满，停止当前会话。"
                ) from exc
            self.turn_queue_metrics.observe(self.turn_queue.qsize(), self.turn_queue.maxsize)
            if self.turn_queue.qsize() / self.turn_queue.maxsize >= 0.8:
                logger.warning(
                    "Turn queue pressure session_id=%s depth=%d max=%d",
                    self.session_id,
                    self.turn_queue.qsize(),
                    self.turn_queue.maxsize,
                )

    async def _send_asr_audio(self, audio: bytes) -> None:
        assert self._active_asr is not None
        try:
            await self._active_asr.send_audio(audio)
        except Exception as exc:
            raise ProtocolError(ErrorCode.ASR_STREAM_FAILED, str(exc)) from exc

    async def _forward_asr_events(
        self, context: TurnContext, asr: RealtimeASRSession
    ) -> tuple[str, str | None]:
        finalized: list[str] = []
        partial = ""
        sequence = 0
        request_id: str | None = None
        async for event in asr.events():
            request_id = event.request_id or request_id
            if event.completed:
                break
            if event.is_sentence_end:
                if event.text and (not finalized or finalized[-1] != event.text):
                    finalized.append(event.text)
                partial = ""
            else:
                partial = event.text
            display = "".join(finalized) + partial
            if not display:
                continue
            sequence += 1
            if context.metrics.asr_first_partial_ms == 0:
                context.metrics.asr_first_partial_ms = (
                    time.monotonic() - context.first_audio_at
                ) * 1000
                logger.info(
                    "ASR first partial session_id=%s turn_id=%s text_length=%d latency_ms=%.1f",
                    self.session_id,
                    context.turn_id,
                    len(display),
                    context.metrics.asr_first_partial_ms,
                )
            await self._send_json(
                {
                    "type": "asr.partial",
                    "session_id": self.session_id,
                    "turn_id": context.turn_id,
                    "text": display,
                    "sequence": sequence,
                    "is_final": False,
                }
            )
        final_text = "".join(finalized) or partial
        return final_text.strip(), request_id

    async def _turn_loop(self) -> None:
        while True:
            stable = await self.turn_queue.get()
            self.turn_queue_metrics.observe(self.turn_queue.qsize(), self.turn_queue.maxsize)
            if stable is None:
                return
            await self._process_stable_turn(stable.context)

    async def _process_stable_turn(self, context: TurnContext) -> None:
        context.state = TurnState.TRANSLATING
        context.translation_started_at = time.monotonic()
        translator = self.dependencies.translator_factory()
        tts = self.dependencies.tts_factory()
        self._active_tts = tts
        self.metrics.provider_calls["translation"] += 1
        self.metrics.provider_calls["tts"] += 1
        segmenter = TTSTextSegmenter(
            self.config.tts_text_min_chars,
            self.config.tts_text_target_chars,
            self.config.tts_text_max_chars,
            self.config.tts_text_max_wait_ms,
        )
        try:
            await tts.start()
        except Exception as exc:
            raise ProtocolError(ErrorCode.TTS_STREAM_FAILED, str(exc)) from exc
        audio_task = asyncio.create_task(self._forward_tts_audio(context, tts))
        tts_text_queue: asyncio.Queue[str | None] = asyncio.Queue(
            self.config.stream_tts_text_queue_max
        )
        text_task = asyncio.create_task(self._forward_tts_text(tts, tts_text_queue))
        translation_sequence = 0
        try:
            async for event in self._translation_events(
                translator, context.recognized_text
            ):
                if event is None:
                    for segment in segmenter.poll():
                        if context.tts_text_started_at == 0:
                            context.tts_text_started_at = time.monotonic()
                        await self._queue_tts_text(text_task, tts_text_queue, segment)
                    continue
                context.translation_request_id = (
                    event.request_id or context.translation_request_id
                )
                if event.final:
                    context.translated_text = event.text
                    context.metrics.translation_final_ms = (
                        time.monotonic() - context.translation_started_at
                    ) * 1000
                    break
                if not event.delta:
                    continue
                translation_sequence += 1
                if context.metrics.translation_first_token_ms == 0:
                    context.metrics.translation_first_token_ms = (
                        time.monotonic() - context.translation_started_at
                    ) * 1000
                    logger.info(
                        "Translation first token session_id=%s turn_id=%s text_length=%d "
                        "latency_ms=%.1f",
                        self.session_id,
                        context.turn_id,
                        len(event.text),
                        context.metrics.translation_first_token_ms,
                    )
                context.translated_text = event.text
                await self._send_json(
                    {
                        "type": "translation.partial",
                        "session_id": self.session_id,
                        "turn_id": context.turn_id,
                        "text": event.text,
                        "delta": event.delta,
                        "sequence": translation_sequence,
                    }
                )
                for segment in segmenter.feed(event.delta):
                    if context.tts_text_started_at == 0:
                        context.tts_text_started_at = time.monotonic()
                    await self._queue_tts_text(text_task, tts_text_queue, segment)
            if not context.translated_text.strip():
                raise ProtocolError(
                    ErrorCode.TRANSLATION_EMPTY_RESULT, "翻译服务返回空文本。"
                )
            await self._send_json(
                {
                    "type": "translation.final",
                    "session_id": self.session_id,
                    "turn_id": context.turn_id,
                    "text": context.translated_text,
                    "provider_request_id": context.translation_request_id,
                }
            )
            logger.info(
                "Translation final session_id=%s turn_id=%s request_id=%s text_length=%d "
                "latency_ms=%.1f",
                self.session_id,
                context.turn_id,
                context.translation_request_id,
                len(context.translated_text),
                context.metrics.translation_final_ms,
            )
            for segment in segmenter.flush():
                if context.tts_text_started_at == 0:
                    context.tts_text_started_at = time.monotonic()
                await self._queue_tts_text(text_task, tts_text_queue, segment)
            try:
                await asyncio.wait_for(tts_text_queue.put(None), timeout=5)
                await text_task
                await tts.complete()
                await audio_task
            except ProtocolError:
                raise
            except Exception as exc:
                raise ProtocolError(ErrorCode.TTS_STREAM_FAILED, str(exc)) from exc
            context.state = TurnState.TURN_COMPLETED
            context.metrics.turn_total_ms = (time.monotonic() - context.started_at) * 1000
            self._turn_count += 1
            logger.info(
                "Turn completed session_id=%s turn_id=%s total_ms=%.1f request_ids=%s",
                self.session_id,
                context.turn_id,
                context.metrics.turn_total_ms,
                {
                    "asr": context.asr_request_id,
                    "translation": context.translation_request_id,
                    "tts": context.tts_request_id,
                },
            )
            await self._send_json(
                {
                    "type": "turn.completed",
                    "session_id": self.session_id,
                    "turn_id": context.turn_id,
                    "recognized_text": context.recognized_text,
                    "translated_text": context.translated_text,
                    "metrics": context.metrics.to_dict(),
                    "provider_request_ids": {
                        "asr": context.asr_request_id,
                        "translation": context.translation_request_id,
                        "tts": context.tts_request_id,
                    },
                }
            )
        except ProtocolError:
            audio_task.cancel()
            text_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await audio_task
            with suppress(asyncio.CancelledError, Exception):
                await text_task
            raise
        except Exception as exc:
            audio_task.cancel()
            text_task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await audio_task
            with suppress(asyncio.CancelledError, Exception):
                await text_task
            raise ProtocolError(ErrorCode.TRANSLATION_FAILED, str(exc)) from exc
        finally:
            self._active_tts = None

    async def _translation_events(
        self, translator: StreamingTranslator, text: str
    ) -> AsyncIterator[TranslationStreamEvent | None]:
        iterator = translator.translate_stream(text, "zh", "en").__aiter__()
        next_event = asyncio.create_task(anext(iterator))
        tick_seconds = self.config.tts_text_max_wait_ms / 1000
        try:
            while True:
                done, _pending = await asyncio.wait({next_event}, timeout=tick_seconds)
                if not done:
                    yield None
                    continue
                try:
                    event = next_event.result()
                except StopAsyncIteration:
                    return
                yield event
                next_event = asyncio.create_task(anext(iterator))
        finally:
            if not next_event.done():
                next_event.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await next_event

    async def _queue_tts_text(
        self,
        text_task: asyncio.Task[None],
        queue: asyncio.Queue[str | None],
        segment: str,
    ) -> None:
        if text_task.done():
            await text_task
        try:
            queue.put_nowait(segment)
        except asyncio.QueueFull as exc:
            raise ProtocolError(
                ErrorCode.SERVER_BACKPRESSURE, "TTS 文本队列已满。"
            ) from exc
        self.tts_text_queue_metrics.observe(queue.qsize(), queue.maxsize)
        if queue.qsize() / queue.maxsize >= 0.8:
            logger.warning(
                "TTS text queue pressure session_id=%s depth=%d max=%d",
                self.session_id,
                queue.qsize(),
                queue.maxsize,
            )

    async def _forward_tts_text(
        self, tts: StreamingTTSSession, queue: asyncio.Queue[str | None]
    ) -> None:
        while True:
            segment = await queue.get()
            self.tts_text_queue_metrics.observe(queue.qsize(), queue.maxsize)
            if segment is None:
                return
            try:
                await tts.send_text(segment)
            except Exception as exc:
                raise ProtocolError(ErrorCode.TTS_STREAM_FAILED, str(exc)) from exc

    async def _forward_tts_audio(
        self, context: TurnContext, tts: StreamingTTSSession
    ) -> None:
        started = False
        audio_bytes = 0
        chunks = 0
        async for event in tts.audio_events():
            context.tts_request_id = event.request_id or context.tts_request_id
            if event.completed:
                break
            if not event.audio or len(event.audio) % tts.sample_width:
                raise ProtocolError(ErrorCode.TTS_AUDIO_INVALID, "TTS PCM 音频无效。")
            if not started:
                started = True
                context.state = TurnState.STREAMING_AUDIO
                context.first_audio_sent_at = time.monotonic()
                if context.tts_text_started_at:
                    context.metrics.tts_first_audio_ms = (
                        context.first_audio_sent_at - context.tts_text_started_at
                    ) * 1000
                context.metrics.server_time_to_first_audio_ms = max(
                    0.0, (context.first_audio_sent_at - context.speech_end_at) * 1000
                )
                await self._send_json(
                    {
                        "type": "tts.audio.start",
                        "session_id": self.session_id,
                        "turn_id": context.turn_id,
                        "format": "pcm_s16le",
                        "sample_rate": tts.sample_rate,
                        "channels": tts.channels,
                        "sample_width": tts.sample_width,
                        "provider_request_id": context.tts_request_id,
                    }
                )
                logger.info(
                    "TTS first audio session_id=%s turn_id=%s request_id=%s bytes=%d "
                    "tts_first_audio_ms=%.1f server_ttfa_ms=%.1f",
                    self.session_id,
                    context.turn_id,
                    context.tts_request_id,
                    len(event.audio),
                    context.metrics.tts_first_audio_ms,
                    context.metrics.server_time_to_first_audio_ms,
                )
            audio_bytes += len(event.audio)
            chunks += 1
            await self._send_bytes(event.audio)
        if not started:
            raise ProtocolError(ErrorCode.TTS_AUDIO_INVALID, "TTS 未返回音频。")
        await self._send_json(
            {
                "type": "tts.audio.end",
                "session_id": self.session_id,
                "turn_id": context.turn_id,
                "audio_bytes": audio_bytes,
                "audio_chunks": chunks,
                "provider_request_id": context.tts_request_id,
            }
        )
        logger.info(
            "TTS audio ended session_id=%s turn_id=%s request_id=%s chunks=%d bytes=%d",
            self.session_id,
            context.turn_id,
            context.tts_request_id,
            chunks,
            audio_bytes,
        )

    async def _send_json(self, payload: dict[str, Any]) -> None:
        async with self._send_lock:
            await self.websocket.send_json(payload)
            self.metrics.websocket_sent_bytes += len(str(payload).encode())

    async def _send_bytes(self, payload: bytes) -> None:
        async with self._send_lock:
            await self.websocket.send_bytes(payload)
            self.metrics.websocket_sent_bytes += len(payload)

    async def _safe_error(self, code: ErrorCode, message: str) -> None:
        logger.warning(
            "Streaming session error session_id=%s client_request_id=%s code=%s",
            self.session_id,
            self.request_id,
            code.value,
        )
        try:
            await self._send_json(
                error_event(
                    session_id=self.session_id,
                    request_id=self.request_id,
                    turn_id=self._active_turn.turn_id if self._active_turn else None,
                    code=code,
                    message=message,
                    recoverable=code == ErrorCode.FALLBACK_REQUIRED,
                )
            )
            await self.websocket.close(code=1011)
        except Exception:
            logger.debug("Unable to send streaming error to disconnected client")


def bearer_token_from_header(authorization: str | None, expected: str) -> str | None:
    prefix = "Bearer "
    if not authorization or not authorization.startswith(prefix):
        return None
    supplied = authorization[len(prefix) :]
    return supplied if secrets.compare_digest(supplied, expected) else None


def _token_fingerprint(token: str) -> str:
    import hashlib

    return hashlib.sha256(token.encode()).hexdigest()[:16]


def _find_protocol_error(group: BaseExceptionGroup[BaseException]) -> ProtocolError | None:
    for exception in group.exceptions:
        if isinstance(exception, ProtocolError):
            return exception
        if isinstance(exception, BaseExceptionGroup):
            nested = _find_protocol_error(exception)
            if nested is not None:
                return nested
    return None


def _group_contains(
    group: BaseExceptionGroup[BaseException], exception_type: type[BaseException]
) -> bool:
    return any(
        isinstance(exception, exception_type)
        or (
            isinstance(exception, BaseExceptionGroup)
            and _group_contains(exception, exception_type)
        )
        for exception in group.exceptions
    )
