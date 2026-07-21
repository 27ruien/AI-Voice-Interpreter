from pathlib import Path

from ai_voice_interpreter.config import AppConfig
from ai_voice_interpreter.pipeline import InterpreterPipeline
from ai_voice_interpreter.providers.mock_providers import (
    MockSpeechRecognizer,
    MockTextToSpeech,
    MockTranslator,
)
from ai_voice_interpreter.ui.workers import ProcessingWorker


class FakePlayer:
    def __init__(self) -> None:
        self.played: Path | None = None

    def play(self, path: Path) -> None:
        self.played = path


def test_processing_worker_exposes_non_ui_core_and_full_status_flow(tmp_path: Path) -> None:
    audio = tmp_path / "input.wav"
    audio.write_bytes(b"mock")
    tts = MockTextToSpeech(tmp_path / "tts")
    pipeline = InterpreterPipeline(MockSpeechRecognizer(), MockTranslator(), tts)
    player = FakePlayer()
    worker = ProcessingWorker(AppConfig(app_mode="mock"), pipeline, player, audio)  # type: ignore[arg-type]
    statuses: list[str] = []
    completed: list[bool] = []
    failures: list[str] = []
    worker.status_changed.connect(statuses.append)
    worker.completed.connect(lambda: completed.append(True))
    worker.failed.connect(failures.append)
    worker.run()
    assert statuses == ["正在识别", "正在翻译", "正在生成语音", "正在播放"]
    assert completed == [True]
    assert failures == []
    assert player.played and player.played.is_file()

