from __future__ import annotations

import logging
import sys

from PySide6.QtWidgets import QApplication, QMessageBox

from .audio.player import MacAudioPlayer
from .audio.recorder import MicrophoneRecorder
from .config import AppConfig
from .exceptions import ConfigurationError
from .logging_config import configure_logging
from .pipeline import InterpreterPipeline
from .providers.dashscope_asr import DashScopeSpeechRecognizer
from .providers.dashscope_translation import DashScopeTranslator
from .providers.dashscope_tts import DashScopeTextToSpeech
from .providers.mock_providers import MockSpeechRecognizer, MockTextToSpeech, MockTranslator
from .ui.main_window import MainWindow

logger = logging.getLogger(__name__)


def build_pipeline(config: AppConfig) -> InterpreterPipeline:
    if config.app_mode == "mock":
        recognizer = MockSpeechRecognizer()
        translator = MockTranslator()
        tts = MockTextToSpeech()
    else:
        recognizer = DashScopeSpeechRecognizer(config)
        translator = DashScopeTranslator(config)
        tts = DashScopeTextToSpeech(config)
    return InterpreterPipeline(
        recognizer=recognizer,
        translator=translator,
        text_to_speech=tts,
        source_language=config.source_language,
        target_language=config.target_language,
        tts_voice=config.effective_tts_voice,
    )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("AI Voice Interpreter")
    app.setOrganizationName("AI Voice Interpreter")
    try:
        config = AppConfig.load()
    except ConfigurationError as exc:
        QMessageBox.critical(None, "配置错误", str(exc))
        return 2
    configure_logging(config.log_level)
    logger.info(
        "Application starting mode=%s asr_model=%s translation_model=%s tts_model=%s",
        config.app_mode,
        config.asr_model,
        config.translation_model,
        config.tts_model,
    )
    recorder = MicrophoneRecorder(
        sample_rate=config.audio_sample_rate,
        channels=config.audio_channels,
        keep_temp_audio=config.keep_temp_audio,
    )
    player = MacAudioPlayer()
    window = MainWindow(config, recorder, player, build_pipeline(config))
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())

