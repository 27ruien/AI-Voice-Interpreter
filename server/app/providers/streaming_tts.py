from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from ai_voice_interpreter.config import AppConfig
from ai_voice_interpreter.providers.common import configure_dashscope, friendly_service_message

from .streaming_interfaces import TTSAudioStreamEvent

logger = logging.getLogger(__name__)


class DashScopeStreamingTTSSession:
    sample_rate = 24000
    channels = 1
    sample_width = 2

    def __init__(self, config: AppConfig, queue_size: int = 100) -> None:
        self.config = config
        self.queue: asyncio.Queue[TTSAudioStreamEvent | Exception] = asyncio.Queue(queue_size)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.synthesizer: Any | None = None
        self.started = False
        self.completed = False
        self.request_id: str | None = None
        self._overflowed = False
        self._pressure_warned = False

    async def start(self) -> None:
        self.loop = asyncio.get_running_loop()
        configure_dashscope(self.config)
        from dashscope.audio.tts_v2 import AudioFormat, ResultCallback, SpeechSynthesizer

        owner = self

        class Callback(ResultCallback):
            def on_data(self, data: bytes) -> None:
                if not data or len(data) % 2:
                    owner._callback_offer(RuntimeError("TTS 返回了无效 PCM 音频。"))
                    return
                owner._refresh_request_id()
                owner._callback_offer(
                    TTSAudioStreamEvent(audio=data, request_id=owner.request_id)
                )

            def on_complete(self) -> None:
                owner._refresh_request_id()
                owner._callback_offer(
                    TTSAudioStreamEvent(completed=True, request_id=owner.request_id)
                )

            def on_error(self, message: Any) -> None:
                owner._callback_offer(RuntimeError(friendly_service_message(message)))

        self.synthesizer = SpeechSynthesizer(
            model=self.config.tts_model,
            voice=self.config.effective_tts_voice,
            format=AudioFormat.PCM_24000HZ_MONO_16BIT,
            language_hints=[self.config.target_language],
            callback=Callback(),
            workspace=self.config.dashscope_workspace_id or None,
        )

    async def send_text(self, text: str) -> None:
        if self.synthesizer is None or self.completed:
            raise RuntimeError("TTS Session 尚未启动或已经结束。")
        if not text:
            return
        self.started = True
        try:
            await asyncio.to_thread(self.synthesizer.streaming_call, text)
            self._refresh_request_id()
        except Exception as exc:
            raise RuntimeError(friendly_service_message(exc)) from exc

    async def complete(self) -> None:
        if self.synthesizer is None or self.completed:
            return
        self.completed = True
        if not self.started:
            self._callback_offer(TTSAudioStreamEvent(completed=True))
            return
        try:
            await asyncio.to_thread(
                self.synthesizer.streaming_complete,
                int(self.config.network_timeout_seconds * 1000),
            )
            self._refresh_request_id()
        except Exception as exc:
            raise RuntimeError(friendly_service_message(exc)) from exc

    async def cancel(self) -> None:
        if self.synthesizer is not None and self.started and not self.completed:
            self.completed = True
            try:
                await asyncio.to_thread(self.synthesizer.streaming_cancel, 5000)
            except Exception:
                logger.exception("Streaming TTS cancellation failed")

    async def audio_events(self):  # type: ignore[no-untyped-def]
        while True:
            event = await self.queue.get()
            if isinstance(event, Exception):
                raise event
            yield event
            if event.completed:
                break

    def _refresh_request_id(self) -> None:
        if self.synthesizer is not None:
            value = self.synthesizer.get_last_request_id()
            if value:
                self.request_id = str(value)

    def _callback_offer(self, event: TTSAudioStreamEvent | Exception) -> None:
        if self.loop is not None:
            self.loop.call_soon_threadsafe(self._offer, event)

    def _offer(self, event: TTSAudioStreamEvent | Exception) -> None:
        try:
            self.queue.put_nowait(event)
            if (
                not self._pressure_warned
                and self.queue.qsize() / self.queue.maxsize >= 0.8
            ):
                self._pressure_warned = True
                logger.warning(
                    "TTS audio queue pressure depth=%d max=%d",
                    self.queue.qsize(),
                    self.queue.maxsize,
                )
        except asyncio.QueueFull:
            if self._overflowed:
                return
            self._overflowed = True
            logger.error("TTS audio queue full")
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.queue.put_nowait(RuntimeError("TTS 音频队列已满。"))
