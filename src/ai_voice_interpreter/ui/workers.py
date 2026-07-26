from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal, Slot

from ..audio.player import MacAudioPlayer
from ..config import AppConfig
from ..meeting.audio_doctor import run_audio_checks
from ..meeting.controller import BridgeState, MeetingBridgeController
from ..meeting.devices import AudioDeviceCatalog, AudioRouteProfile
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


class MeetingBridgeWorker(QObject):
    event_received = Signal(str, object)
    snapshot_changed = Signal(object)
    state_changed = Signal(str)
    completed = Signal()
    failed = Signal(str)
    finished = Signal()

    def __init__(self, controller: MeetingBridgeController) -> None:
        super().__init__()
        self.controller = controller
        self.controller.event_callback = self._emit_event
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    @Slot()
    def run(self) -> None:
        try:
            self.controller.start()
            self.state_changed.emit(self.controller.state.value)
            while not self._stop.wait(0.1):
                self.state_changed.emit(self.controller.state.value)
                self.snapshot_changed.emit(self.controller.snapshot())
                if self.controller.state in {BridgeState.FAILED, BridgeState.STOPPED}:
                    break
            self.controller.stop()
            self.state_changed.emit(self.controller.state.value)
            if self.controller.state == BridgeState.FAILED:
                self.failed.emit("Meeting Bridge 两个方向均已失败。")
            else:
                self.completed.emit()
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.controller.stop()
            self.finished.emit()

    def _emit_event(self, direction: str, event: dict[str, Any]) -> None:
        self.event_received.emit(direction, event)


class MeetingAudioCheckWorker(QObject):
    report_ready = Signal(object)
    failed = Signal(str)
    finished = Signal()

    def __init__(
        self,
        catalog: AudioDeviceCatalog,
        profile: AudioRouteProfile,
    ) -> None:
        super().__init__()
        self.catalog = catalog
        self.profile = profile

    @Slot()
    def run(self) -> None:
        try:
            self.report_ready.emit(
                run_audio_checks(catalog=self.catalog, profile=self.profile)
            )
        except Exception as exc:
            self.failed.emit(str(exc))
        finally:
            self.finished.emit()
