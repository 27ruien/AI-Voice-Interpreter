from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from ..audio.player import MacAudioPlayer
from ..config import AppConfig
from ..models import PipelineResult, ProcessingStatus
from ..streaming.controller import StreamingSessionController


class ProcessingWorker(QObject):
    status_changed = Signal(str)
    result_ready = Signal(object)
    completed = Signal()
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        config: AppConfig,
        pipeline: Any,
        player: MacAudioPlayer,
        audio_path: Path,
    ) -> None:
        super().__init__()
        self.config = config
        self.pipeline = pipeline
        self.player = player
        self.audio_path = audio_path

    @Slot()
    def run(self) -> None:
        result: PipelineResult | None = None
        result_emitted = False
        try:
            self.config.validate_for_processing()
            result = self.pipeline.process(
                self.audio_path,
                on_status=lambda status: self.status_changed.emit(status.value),
            )
            self.result_ready.emit(result)
            result_emitted = True
            if result.error:
                self.failed.emit(result.error)
                return
            if result.generated_audio_path is None:
                self.failed.emit("语音生成未返回可播放文件。")
                return
            self.status_changed.emit(ProcessingStatus.PLAYING.value)
            self.player.play(result.generated_audio_path)
            self.completed.emit()
        except Exception as exc:
            if not result_emitted:
                result = PipelineResult(error=str(exc))
                self.result_ready.emit(result)
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class PlaybackWorker(QObject):
    completed = Signal()
    failed = Signal(str)
    finished = Signal()

    def __init__(self, player: MacAudioPlayer, audio_path: Path) -> None:
        super().__init__()
        self.player = player
        self.audio_path = audio_path

    @Slot()
    def run(self) -> None:
        try:
            self.player.play(self.audio_path)
            self.completed.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()


class StreamingWorker(QObject):
    event_received = Signal(object)
    audio_ready = Signal(object)
    completed = Signal()
    failed = Signal(str)
    finished = Signal()

    def __init__(self, config: AppConfig, fallback_pipeline: Any, player: MacAudioPlayer) -> None:
        super().__init__()
        self.controller = StreamingSessionController(
            config,
            fallback_pipeline=fallback_pipeline,
            fallback_player=player,
        )

    def request_stop(self) -> None:
        self.controller.request_stop()

    @Slot()
    def run(self) -> None:
        try:
            self.controller.run(
                on_event=self.event_received.emit,
                on_audio_ready=self.audio_ready.emit,
            )
            self.completed.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            if self.controller.stream_player.last_turn_path is None:
                self.controller.cleanup()
            self.finished.emit()
