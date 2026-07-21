from PySide6.QtWidgets import QApplication

from ai_voice_interpreter.audio import MacAudioPlayer, MicrophoneRecorder
from ai_voice_interpreter.config import AppConfig
from ai_voice_interpreter.main import build_pipeline
from ai_voice_interpreter.ui import MainWindow


def test_real_gui_starts_and_shows_missing_key_error() -> None:
    app = QApplication.instance() or QApplication([])
    config = AppConfig(app_mode="real", dashscope_api_key="")
    window = MainWindow(
        config,
        MicrophoneRecorder(),
        MacAudioPlayer(),
        build_pipeline(config),
    )
    assert "DASHSCOPE_API_KEY" in window.error_text.toPlainText()
    assert window.start_button.isEnabled()
    assert not window.stop_button.isEnabled()
    window.close()
    app.processEvents()

