from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path

from .exceptions import InterpreterError
from .models import PipelineResult, ProcessingStatus
from .providers.interfaces import SpeechRecognizer, TextToSpeech, Translator

logger = logging.getLogger(__name__)
StatusCallback = Callable[[ProcessingStatus], None]


class InterpreterPipeline:
    def __init__(
        self,
        recognizer: SpeechRecognizer,
        translator: Translator,
        text_to_speech: TextToSpeech,
        source_language: str = "zh",
        target_language: str = "en",
        tts_voice: str | None = None,
    ) -> None:
        self.recognizer = recognizer
        self.translator = translator
        self.text_to_speech = text_to_speech
        self.source_language = source_language
        self.target_language = target_language
        self.tts_voice = tts_voice

    def process(
        self,
        audio_path: Path,
        on_status: StatusCallback | None = None,
    ) -> PipelineResult:
        started = time.perf_counter()
        result = PipelineResult()
        notify = on_status or (lambda _status: None)
        try:
            notify(ProcessingStatus.RECOGNIZING)
            logger.info("ASR started")
            asr = self.recognizer.transcribe(audio_path)
            result.recognized_text = asr.text
            result.asr_latency_ms = max(0.0, asr.duration_ms)
            self._record_metadata(result, "asr", asr.provider, asr.model, asr.request_id)

            notify(ProcessingStatus.TRANSLATING)
            logger.info("Translation started")
            translation = self.translator.translate(
                asr.text,
                self.source_language,
                self.target_language,
                context=None,
            )
            result.translated_text = translation.translated_text
            result.translation_latency_ms = max(0.0, translation.duration_ms)
            self._record_metadata(
                result,
                "translation",
                translation.provider,
                translation.model,
                translation.request_id,
            )

            notify(ProcessingStatus.SYNTHESIZING)
            logger.info("TTS started")
            tts = self.text_to_speech.synthesize(translation.translated_text, self.tts_voice)
            result.generated_audio_path = tts.audio_path
            result.tts_latency_ms = max(0.0, tts.duration_ms)
            self._record_metadata(result, "tts", tts.provider, tts.model, tts.request_id)
        except InterpreterError as exc:
            result.error = str(exc)
            logger.exception("Pipeline failed type=%s", type(exc).__name__)
        except Exception as exc:
            result.error = f"处理失败：{exc}"
            logger.exception("Pipeline failed unexpectedly type=%s", type(exc).__name__)
        finally:
            result.total_latency_ms = max(0.0, (time.perf_counter() - started) * 1000)
            logger.info(
                "Pipeline finished total_ms=%.1f success=%s",
                result.total_latency_ms,
                result.succeeded,
            )
        return result

    @staticmethod
    def _record_metadata(
        result: PipelineResult,
        stage: str,
        provider: str,
        model: str,
        request_id: str | None,
    ) -> None:
        result.providers[stage] = provider
        result.models[stage] = model
        if request_id:
            result.request_ids[stage] = request_id
