from __future__ import annotations

import asyncio
from http import HTTPStatus

from ai_voice_interpreter.config import AppConfig
from ai_voice_interpreter.providers.common import (
    configure_dashscope,
    friendly_service_message,
    request_id_from,
)
from ai_voice_interpreter.providers.dashscope_translation import LANGUAGE_NAMES, _extract_content
from ai_voice_interpreter.streaming.normalizer import DeltaNormalizer

from .streaming_interfaces import TranslationStreamEvent


class DashScopeStreamingTranslator:
    def __init__(self, config: AppConfig, queue_size: int = 50) -> None:
        self.config = config
        self.queue_size = queue_size

    async def translate_stream(
        self,
        text: str,
        source_language: str,
        target_language: str,
    ):
        if not text.strip():
            raise RuntimeError("稳定 Source Turn 为空，无法翻译。")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[TranslationStreamEvent | Exception | None] = asyncio.Queue(
            self.queue_size
        )
        producer = asyncio.create_task(
            asyncio.to_thread(
                self._produce,
                loop,
                queue,
                text,
                source_language,
                target_language,
            )
        )
        try:
            while True:
                event = await queue.get()
                if event is None:
                    break
                if isinstance(event, Exception):
                    raise event
                yield event
        finally:
            await producer

    def _produce(
        self,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[TranslationStreamEvent | Exception | None],
        text: str,
        source_language: str,
        target_language: str,
    ) -> None:
        normalizer = DeltaNormalizer(cumulative=False)
        request_id: str | None = None
        try:
            dashscope = configure_dashscope(self.config)
            responses = dashscope.Generation.call(
                api_key=self.config.dashscope_api_key,
                model=self.config.translation_model,
                messages=[{"role": "user", "content": text.strip()}],
                translation_options={
                    "source_lang": LANGUAGE_NAMES.get(source_language, source_language),
                    "target_lang": LANGUAGE_NAMES.get(target_language, target_language),
                },
                result_format="message",
                stream=True,
                incremental_output=True,
                timeout=self.config.network_timeout_seconds,
                workspace=self.config.dashscope_workspace_id or None,
            )
            for response in responses:
                request_id = request_id_from(response) or request_id
                if response.status_code != HTTPStatus.OK:
                    raise RuntimeError(
                        friendly_service_message(response.message, response.status_code)
                    )
                chunk = _extract_content(response)
                normalized = normalizer.push(chunk)
                if normalized.delta:
                    self._put(
                        loop,
                        queue,
                        TranslationStreamEvent(
                            text=normalized.text,
                            delta=normalized.delta,
                            request_id=request_id,
                        ),
                    )
            if not normalizer.text.strip():
                raise RuntimeError("翻译服务返回空结果。")
            self._put(
                loop,
                queue,
                TranslationStreamEvent(
                    text=normalizer.text,
                    delta="",
                    request_id=request_id,
                    final=True,
                ),
            )
        except Exception as exc:
            self._put(loop, queue, RuntimeError(friendly_service_message(exc)))
        finally:
            self._put(loop, queue, None)

    @staticmethod
    def _put(
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue[TranslationStreamEvent | Exception | None],
        item: TranslationStreamEvent | Exception | None,
    ) -> None:
        future = asyncio.run_coroutine_threadsafe(queue.put(item), loop)
        future.result(timeout=10)
