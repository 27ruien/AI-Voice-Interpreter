from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any

from ..config import AppConfig
from ..exceptions import TranslationProviderError
from ..models import TranslationResult
from .common import configure_dashscope, friendly_service_message, request_id_from

logger = logging.getLogger(__name__)
LANGUAGE_NAMES = {"zh": "Chinese", "en": "English"}


class DashScopeTranslator:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def translate(
        self,
        text: str,
        source_language: str,
        target_language: str,
        context: Mapping[str, object] | None = None,
    ) -> TranslationResult:
        del context  # Reserved for glossary, previous turns and target style in the next phase.
        if not text.strip():
            raise TranslationProviderError("识别文本为空，无法翻译。")
        started = time.perf_counter()
        try:
            dashscope = configure_dashscope(self.config)
            response = dashscope.Generation.call(
                api_key=self.config.dashscope_api_key,
                model=self.config.translation_model,
                messages=[{"role": "user", "content": text.strip()}],
                translation_options={
                    "source_lang": LANGUAGE_NAMES.get(source_language, source_language),
                    "target_lang": LANGUAGE_NAMES.get(target_language, target_language),
                },
                result_format="message",
                timeout=self.config.network_timeout_seconds,
            )
            request_id = request_id_from(response)
            if response.status_code != HTTPStatus.OK:
                logger.error(
                    "Translation request_id=%s status=%s message=%s",
                    request_id,
                    response.status_code,
                    response.message,
                )
                raise TranslationProviderError(
                    friendly_service_message(response.message, response.status_code)
                )
            translated = _extract_content(response).strip()
            if not translated:
                raise TranslationProviderError("翻译服务返回了空文本，请重试。")
            duration_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "Translation completed elapsed_ms=%.1f request_id=%s",
                duration_ms,
                request_id,
            )
            logger.debug("Translation text=%r", translated)
            return TranslationResult(
                source_text=text,
                translated_text=translated,
                source_language=source_language,
                target_language=target_language,
                duration_ms=duration_ms,
                provider="dashscope",
                model=self.config.translation_model,
                request_id=request_id,
            )
        except TranslationProviderError:
            raise
        except Exception as exc:
            logger.exception("Translation failed type=%s", type(exc).__name__)
            raise TranslationProviderError(friendly_service_message(exc)) from exc


def _extract_content(response: Any) -> str:
    content = response.output.choices[0].message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            str(item.get("text", "")) if isinstance(item, dict) else str(item) for item in content
        )
    return str(content)
