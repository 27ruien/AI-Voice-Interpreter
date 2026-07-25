from __future__ import annotations

import asyncio
import contextlib
import logging
from http import HTTPStatus
from typing import Any

from ai_voice_interpreter.config import AppConfig
from ai_voice_interpreter.providers.common import (
    configure_dashscope,
    friendly_service_message,
    request_id_from,
)

from .streaming_interfaces import ASRStreamEvent

logger = logging.getLogger(__name__)


class DashScopeRealtimeASRSession:
    def __init__(self, config: AppConfig, queue_size: int = 50) -> None:
        self.config = config
        self.queue: asyncio.Queue[ASRStreamEvent | Exception] = asyncio.Queue(queue_size)
        self.recognition: Any | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self._finished = False
        self._last_partial = ""
        self.request_id: str | None = None
        self._overflowed = False
        self._pressure_warned = False

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        configure_dashscope(self.config)
        from dashscope.audio.asr import Recognition, RecognitionCallback

        owner = self

        class Callback(RecognitionCallback):
            def on_event(self, result: Any) -> None:
                owner._callback_event(result)

            def on_complete(self) -> None:
                owner._callback_offer(ASRStreamEvent(completed=True, request_id=owner.request_id))

            def on_error(self, result: Any) -> None:
                owner.request_id = request_id_from(result) or owner.request_id
                owner._finished = True
                owner._callback_offer(RuntimeError(friendly_service_message(result)))

        kwargs: dict[str, Any] = {}
        if self.config.dashscope_workspace_id:
            kwargs["workspace"] = self.config.dashscope_workspace_id
        self.recognition = Recognition(
            model=self.config.asr_model,
            callback=Callback(),
            format="pcm",
            sample_rate=16000,
            language_hints=[self.config.source_language],
            **kwargs,
        )
        try:
            await asyncio.to_thread(self.recognition.start)
        except Exception as exc:
            raise RuntimeError(friendly_service_message(exc)) from exc

    async def send_audio(self, pcm: bytes) -> None:
        if self.recognition is None or self._finished:
            raise RuntimeError("ASR Session 尚未启动或已经结束。")
        try:
            await asyncio.to_thread(self.recognition.send_audio_frame, pcm)
        except Exception as exc:
            raise RuntimeError(friendly_service_message(exc)) from exc

    async def finish(self) -> None:
        if self.recognition is None or self._finished:
            return
        self._finished = True
        try:
            await asyncio.to_thread(self.recognition.stop)
        except Exception as exc:
            raise RuntimeError(friendly_service_message(exc)) from exc

    async def cancel(self) -> None:
        if self.recognition is not None and not self._finished:
            try:
                await self.finish()
            except Exception:
                logger.exception("Realtime ASR cancellation failed")

    async def events(self):  # type: ignore[no-untyped-def]
        while True:
            event = await self.queue.get()
            if isinstance(event, Exception):
                raise event
            yield event
            if event.completed:
                break

    def _callback_event(self, result: Any) -> None:
        if getattr(result, "status_code", HTTPStatus.OK) != HTTPStatus.OK:
            self._callback_offer(RuntimeError(friendly_service_message(result)))
            return
        request_id = getattr(result, "request_id", None)
        if request_id:
            self.request_id = str(request_id)
        sentences = result.get_sentence() or []
        if isinstance(sentences, dict):
            sentences = [sentences]
        for sentence in sentences:
            text = str(sentence.get("text", "")).strip()
            if not text or text == self._last_partial:
                continue
            self._last_partial = text
            self._callback_offer(
                ASRStreamEvent(
                    text=text,
                    is_sentence_end=result.is_sentence_end(sentence),
                    request_id=self.request_id,
                )
            )

    def _callback_offer(self, event: ASRStreamEvent | Exception) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._offer, event)

    def _offer(self, event: ASRStreamEvent | Exception) -> None:
        try:
            self.queue.put_nowait(event)
            if (
                not self._pressure_warned
                and self.queue.qsize() / self.queue.maxsize >= 0.8
            ):
                self._pressure_warned = True
                logger.warning(
                    "ASR event queue pressure depth=%d max=%d",
                    self.queue.qsize(),
                    self.queue.maxsize,
                )
        except asyncio.QueueFull:
            if self._overflowed:
                return
            self._overflowed = True
            logger.error("ASR event queue full")
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.queue.put_nowait(RuntimeError("ASR 事件队列已满。"))
