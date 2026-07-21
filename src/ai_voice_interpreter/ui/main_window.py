from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..audio.player import MacAudioPlayer
from ..audio.recorder import MicrophoneRecorder
from ..config import AppConfig
from ..exceptions import InterpreterError
from ..models import PipelineResult, ProcessingStatus
from ..pipeline import InterpreterPipeline
from .workers import PlaybackWorker, ProcessingWorker

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(
        self,
        config: AppConfig,
        recorder: MicrophoneRecorder,
        player: MacAudioPlayer,
        pipeline: InterpreterPipeline,
    ) -> None:
        super().__init__()
        self.config = config
        self.recorder = recorder
        self.player = player
        self.pipeline = pipeline
        self.latest_audio_path: Path | None = None
        self._thread: QThread | None = None
        self._worker: Any | None = None
        self._build_ui()
        self._set_status(ProcessingStatus.READY)
        self._show_startup_notice()

    def _build_ui(self) -> None:
        self.setWindowTitle("AI Voice Interpreter")
        self.setMinimumSize(820, 720)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        title = QLabel("AI Voice Interpreter")
        title.setFont(QFont("", 24, QFont.Weight.Bold))
        subtitle = QLabel("按句语音翻译 MVP · 中文 → English")
        subtitle.setStyleSheet("color: #64748b; font-size: 14px;")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        if self.config.app_mode == "mock":
            banner = QLabel("Mock Mode：识别与翻译为固定示例，播放的是本地测试音，不调用外部 API。")
            banner.setWordWrap(True)
            banner.setStyleSheet(
                "background: #fff7ed; color: #9a3412; padding: 10px; border-radius: 6px;"
            )
            layout.addWidget(banner)

        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("当前状态"))
        self.status_label = QLabel()
        self.status_label.setStyleSheet(
            "background: #e0f2fe; color: #075985; padding: 6px 12px; border-radius: 12px;"
        )
        status_row.addWidget(self.status_label)
        status_row.addStretch()
        layout.addLayout(status_row)

        button_row = QHBoxLayout()
        self.start_button = QPushButton("开始录音")
        self.stop_button = QPushButton("停止并翻译")
        self.replay_button = QPushButton("重新播放")
        self.start_button.clicked.connect(self._start_recording)
        self.stop_button.clicked.connect(self._stop_and_translate)
        self.replay_button.clicked.connect(self._replay)
        self.stop_button.setEnabled(False)
        self.replay_button.setEnabled(False)
        for button in (self.start_button, self.stop_button, self.replay_button):
            button.setMinimumHeight(42)
            button.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            button_row.addWidget(button)
        layout.addLayout(button_row)

        text_grid = QGridLayout()
        text_grid.setHorizontalSpacing(16)
        text_grid.addWidget(QLabel("中文识别结果"), 0, 0)
        text_grid.addWidget(QLabel("英文翻译结果"), 0, 1)
        self.source_text = QTextEdit()
        self.target_text = QTextEdit()
        for editor in (self.source_text, self.target_text):
            editor.setReadOnly(True)
            editor.setPlaceholderText("等待处理…")
            editor.setMinimumHeight(160)
        text_grid.addWidget(self.source_text, 1, 0)
        text_grid.addWidget(self.target_text, 1, 1)
        layout.addLayout(text_grid)

        metrics_frame = QFrame()
        metrics_frame.setFrameShape(QFrame.Shape.StyledPanel)
        metrics_layout = QGridLayout(metrics_frame)
        self.latency_labels: dict[str, QLabel] = {}
        metrics = (
            ("asr", "ASR 延迟"),
            ("translation", "翻译延迟"),
            ("tts", "TTS 延迟"),
            ("total", "总延迟"),
        )
        for column, (key, label) in enumerate(metrics):
            metrics_layout.addWidget(QLabel(label), 0, column, Qt.AlignmentFlag.AlignCenter)
            value = QLabel("—")
            value.setFont(QFont("", 15, QFont.Weight.Bold))
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            metrics_layout.addWidget(value, 1, column)
            self.latency_labels[key] = value
        layout.addWidget(metrics_frame)

        layout.addWidget(QLabel("错误与配置提示"))
        self.error_text = QTextEdit()
        self.error_text.setReadOnly(True)
        self.error_text.setMaximumHeight(100)
        self.error_text.setStyleSheet("color: #b91c1c;")
        layout.addWidget(self.error_text)
        self.setCentralWidget(root)

    def _show_startup_notice(self) -> None:
        if self.config.app_mode == "real" and not self.config.dashscope_api_key:
            self.error_text.setPlainText(
                "真实模式尚未配置 DASHSCOPE_API_KEY。GUI 可以使用，但停止录音后不会调用服务。"
                "请编辑 .env，或退出后执行 make mock。"
            )

    def _start_recording(self) -> None:
        if self._thread is not None:
            return
        try:
            self.player.stop()
            self.error_text.clear()
            self.source_text.clear()
            self.target_text.clear()
            self._reset_latencies()
            self.recorder.start()
            self._set_status(ProcessingStatus.RECORDING)
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.replay_button.setEnabled(False)
        except InterpreterError as exc:
            self._fail(str(exc))

    def _stop_and_translate(self) -> None:
        try:
            audio_path = self.recorder.stop()
        except InterpreterError as exc:
            self._fail(str(exc))
            return
        self.start_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        self.replay_button.setEnabled(False)
        worker = ProcessingWorker(self.config, self.pipeline, self.player, audio_path)
        worker.status_changed.connect(self.status_label.setText)
        worker.result_ready.connect(self._display_result)
        worker.completed.connect(self._complete)
        worker.failed.connect(self._fail)
        self._start_worker(worker)

    def _replay(self) -> None:
        if self.latest_audio_path is None or self._thread is not None:
            self._fail("没有可重新播放的音频。")
            return
        self._set_status(ProcessingStatus.PLAYING)
        self.start_button.setEnabled(False)
        self.replay_button.setEnabled(False)
        worker = PlaybackWorker(self.player, self.latest_audio_path)
        worker.completed.connect(self._complete)
        worker.failed.connect(self._fail)
        self._start_worker(worker)

    def _start_worker(self, worker: Any) -> None:
        thread = QThread(self)
        self._thread = thread
        self._worker = worker
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._worker_thread_finished)
        thread.start()

    def _worker_thread_finished(self) -> None:
        self._thread = None
        self._worker = None

    def _display_result(self, result: PipelineResult) -> None:
        self.source_text.setPlainText(result.recognized_text)
        self.target_text.setPlainText(result.translated_text)
        self.latency_labels["asr"].setText(_format_ms(result.asr_latency_ms))
        self.latency_labels["translation"].setText(_format_ms(result.translation_latency_ms))
        self.latency_labels["tts"].setText(_format_ms(result.tts_latency_ms))
        self.latency_labels["total"].setText(_format_ms(result.total_latency_ms))
        if result.generated_audio_path is not None:
            self.latest_audio_path = result.generated_audio_path

    def _complete(self) -> None:
        self._set_status(ProcessingStatus.COMPLETED)
        self.error_text.clear()
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.replay_button.setEnabled(self.latest_audio_path is not None)

    def _fail(self, message: str) -> None:
        self._set_status(ProcessingStatus.FAILED)
        self.error_text.setPlainText(message or "发生未知错误，请重试。")
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.replay_button.setEnabled(
            self.latest_audio_path is not None and self.latest_audio_path.is_file()
        )

    def _set_status(self, status: ProcessingStatus) -> None:
        self.status_label.setText(status.value)

    def _reset_latencies(self) -> None:
        for label in self.latency_labels.values():
            label.setText("—")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 (Qt API)
        logger.info("Application window closing")
        self.player.stop()
        if self._thread is not None and self._thread.isRunning():
            self._thread.quit()
            if not self._thread.wait(3000):
                self.error_text.setPlainText("后台请求仍在结束中，请稍候再次关闭窗口。")
                event.ignore()
                return
        self.recorder.cleanup()
        cleanup = getattr(self.pipeline.text_to_speech, "cleanup", None)
        if callable(cleanup):
            cleanup()
        event.accept()


def _format_ms(value: float) -> str:
    return f"{max(0.0, value):.0f} ms"
