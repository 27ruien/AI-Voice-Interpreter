import logging
from pathlib import Path

from ai_voice_interpreter.exceptions import (
    ASRProviderError,
    TranslationProviderError,
    TTSProviderError,
)
from ai_voice_interpreter.models import ASRResult, TranslationResult, TTSResult
from ai_voice_interpreter.pipeline import InterpreterPipeline
from ai_voice_interpreter.providers.mock_providers import (
    MockSpeechRecognizer,
    MockTextToSpeech,
    MockTranslator,
)


class NeverTranslator:
    called = False

    def translate(self, *args: object, **kwargs: object) -> TranslationResult:
        self.called = True
        raise AssertionError("translator should not run")


class NeverTTS:
    called = False

    def synthesize(self, *args: object, **kwargs: object) -> TTSResult:
        self.called = True
        raise AssertionError("TTS should not run")


class FailingASR:
    def transcribe(self, _audio_path: Path) -> ASRResult:
        raise ASRProviderError("ASR 测试失败")


class FailingTranslator:
    def translate(self, *args: object, **kwargs: object) -> TranslationResult:
        raise TranslationProviderError("翻译测试失败")


class FailingTTS:
    def synthesize(self, *args: object, **kwargs: object) -> TTSResult:
        raise TTSProviderError("TTS 测试失败")


def audio_file(tmp_path: Path) -> Path:
    path = tmp_path / "input.wav"
    path.write_bytes(b"mock input")
    return path


def test_pipeline_normal_flow_and_latencies(tmp_path: Path) -> None:
    statuses: list[str] = []
    pipeline = InterpreterPipeline(
        MockSpeechRecognizer(),
        MockTranslator(),
        MockTextToSpeech(tmp_path / "tts"),
    )
    result = pipeline.process(audio_file(tmp_path), lambda status: statuses.append(status.value))
    assert result.succeeded
    assert result.recognized_text
    assert result.translated_text
    assert result.generated_audio_path and result.generated_audio_path.is_file()
    assert statuses == ["正在识别", "正在翻译", "正在生成语音"]
    assert all(
        value >= 0
        for value in (
            result.asr_latency_ms,
            result.translation_latency_ms,
            result.tts_latency_ms,
            result.total_latency_ms,
        )
    )
    assert result.providers == {"asr": "mock", "translation": "mock", "tts": "mock"}


def test_asr_failure_stops_later_stages(tmp_path: Path) -> None:
    translator = NeverTranslator()
    tts = NeverTTS()
    result = InterpreterPipeline(FailingASR(), translator, tts).process(audio_file(tmp_path))
    assert result.error == "ASR 测试失败"
    assert not translator.called
    assert not tts.called


def test_translation_failure_stops_tts(tmp_path: Path) -> None:
    tts = NeverTTS()
    result = InterpreterPipeline(MockSpeechRecognizer(), FailingTranslator(), tts).process(
        audio_file(tmp_path)
    )
    assert result.error == "翻译测试失败"
    assert result.recognized_text
    assert not tts.called


def test_tts_failure_is_explicit(tmp_path: Path) -> None:
    result = InterpreterPipeline(MockSpeechRecognizer(), MockTranslator(), FailingTTS()).process(
        audio_file(tmp_path)
    )
    assert result.error == "TTS 测试失败"
    assert result.translated_text
    assert result.generated_audio_path is None


def test_pipeline_info_logs_have_ids_lengths_paths_and_latencies(
    tmp_path: Path,
    caplog: object,
) -> None:
    caplog.set_level(logging.INFO)  # type: ignore[attr-defined]
    pipeline = InterpreterPipeline(
        MockSpeechRecognizer(),
        MockTranslator(),
        MockTextToSpeech(tmp_path / "tts-observability"),
    )
    result = pipeline.process(audio_file(tmp_path))
    assert result.succeeded
    messages = "\n".join(record.getMessage() for record in caplog.records)  # type: ignore[attr-defined]
    assert "ASR finished latency_ms=" in messages
    assert "request_id=mock-asr-request" in messages
    assert "text_length=" in messages
    assert "Translation finished latency_ms=" in messages
    assert "request_id=mock-translation-request" in messages
    assert "TTS finished latency_ms=" in messages
    assert "request_id=mock-tts-request" in messages
    assert "audio_path=" in messages
    assert "audio_bytes=" in messages
    assert "Pipeline finished total_ms=" in messages
