from __future__ import annotations

import logging
import time
from http import HTTPStatus
from pathlib import Path

from ..config import AppConfig
from ..exceptions import ASRProviderError
from ..models import ASRResult
from .common import configure_dashscope, friendly_service_message, request_id_from

logger = logging.getLogger(__name__)


class DashScopeSpeechRecognizer:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def transcribe(self, audio_path: Path) -> ASRResult:
        if not audio_path.is_file():
            raise ASRProviderError(f"录音文件不存在：{audio_path}")
        started = time.perf_counter()
        logger.info(
            "ASR provider started model=%s audio_file=%s audio_bytes=%d",
            self.config.asr_model,
            audio_path.name,
            audio_path.stat().st_size,
        )
        try:
            configure_dashscope(self.config)
            from dashscope.audio.asr import Recognition

            recognition = Recognition(
                model=self.config.asr_model,
                format="wav",
                sample_rate=self.config.audio_sample_rate,
                language_hints=[self.config.source_language],
                callback=None,
            )
            response = recognition.call(str(audio_path))
            request_id = recognition.get_last_request_id() or request_id_from(response)
            if response.status_code != HTTPStatus.OK:
                logger.error(
                    "ASR request_id=%s status=%s message=%s",
                    request_id,
                    response.status_code,
                    response.message,
                )
                raise ASRProviderError(
                    friendly_service_message(response.message, response.status_code)
                )
            sentences = response.get_sentence() or []
            if isinstance(sentences, dict):
                sentences = [sentences]
            text = "".join(str(sentence.get("text", "")) for sentence in sentences).strip()
            if not text:
                raise ASRProviderError("未识别到有效中文语音，请靠近麦克风后重试。")
            duration_ms = (time.perf_counter() - started) * 1000
            logger.info("ASR completed elapsed_ms=%.1f request_id=%s", duration_ms, request_id)
            logger.info("ASR output text_length=%d empty=%s", len(text), not bool(text))
            logger.debug("ASR text=%r", text)
            return ASRResult(
                text=text,
                language=self.config.source_language,
                duration_ms=duration_ms,
                provider="dashscope",
                model=self.config.asr_model,
                request_id=request_id,
            )
        except ASRProviderError:
            raise
        except Exception as exc:
            logger.exception("ASR failed type=%s", type(exc).__name__)
            raise ASRProviderError(friendly_service_message(exc)) from exc
