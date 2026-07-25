from __future__ import annotations

from pathlib import Path

from ai_voice_interpreter.pipeline import InterpreterPipeline
from ai_voice_interpreter.providers.dashscope_asr import DashScopeSpeechRecognizer
from ai_voice_interpreter.providers.dashscope_translation import DashScopeTranslator
from ai_voice_interpreter.providers.dashscope_tts import DashScopeTextToSpeech

from .config import ServerConfig


def build_server_pipeline(config: ServerConfig) -> InterpreterPipeline:
    provider_config = config.provider_config()
    return InterpreterPipeline(
        recognizer=DashScopeSpeechRecognizer(provider_config),
        translator=DashScopeTranslator(provider_config),
        text_to_speech=DashScopeTextToSpeech(provider_config, output_dir=config.temp_audio_dir),
        source_language="zh",
        target_language="en",
        tts_voice=provider_config.effective_tts_voice,
    )


def generated_audio_size(path: Path) -> int:
    return path.stat().st_size if path.is_file() else 0
