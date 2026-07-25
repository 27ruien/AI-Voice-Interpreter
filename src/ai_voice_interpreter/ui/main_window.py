from __future__ import annotations

import logging
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QComboBox,
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
from .workers import PlaybackWorker, ProcessingWorker, StreamingWorker

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(
        self,
        config: AppConfig,
        recorder: MicrophoneRecorder,
        player: MacAudioPlayer,
        pipeline: Any,
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
        self.setMinimumSize(1050, 760)
        root = QWidget()
        layout = QVBoxLayout(root)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        title = QLabel("AI Voice Interpreter")
        title.setFont(QFont("", 24, QFont.Weight.Bold))
        subtitle = QLabel("Turn-based Streaming 语音翻译 MVP · 中文 → English")
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
        elif self.config.interpreter_mode in {"remote", "remote_stream"}:
            banner = QLabel(
                "Remote Mode：录音将通过 HTTPS Gateway 处理；Mac 不保存 DashScope API Key。"
            )
            banner.setWordWrap(True)
            banner.setStyleSheet(
                "background: #ecfdf5; color: #065f46; padding: 10px; border-radius: 6px;"
            )
            layout.addWidget(banner)

        mode_row = QHBoxLayout()
        mode_row.addWidget(QLabel("工作模式"))
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("流式模式", "remote_stream")
        self.mode_combo.addItem("按句模式", "remote")
        selected = (
            "remote_stream"
            if self.config.app_mode == "real" and self.config.interpreter_mode == "remote_stream"
            else "remote"
        )
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(selected))
        if self.config.app_mode == "mock":
            self.mode_combo.setItemData(0, False, Qt.ItemDataRole.UserRole - 1)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        mode_row.addWidget(self.mode_combo)
        mode_row.addWidget(QLabel("采集模式"))
        self.capture_combo = QComboBox()
        self.capture_combo.addItem("Safe Mode（播放时暂停麦克风）", "safe")
        self.capture_combo.addItem("Headphones Mode（建议佩戴耳机）", "headphones")
        self.capture_combo.setCurrentIndex(
            self.capture_combo.findData(self.config.stream_capture_mode)
        )
        mode_row.addWidget(self.capture_combo)
        mode_row.addStretch()
        layout.addLayout(mode_row)

        status_row = QHBoxLayout()
        status_row.addWidget(QLabel("当前状态"))
        self.status_label = QLabel()
        self.status_label.setStyleSheet(
            "background: #e0f2fe; color: #075985; padding: 6px 12px; border-radius: 12px;"
        )
        status_row.addWidget(self.status_label)
        status_row.addStretch()
        layout.addLayout(status_row)

        stream_status = QGridLayout()
        self.stream_labels: dict[str, QLabel] = {}
        for index, (key, title_text) in enumerate(
            (
                ("connection", "连接"),
                ("microphone", "麦克风"),
                ("vad", "VAD"),
                ("turn", "Turn"),
                ("fallback", "Fallback"),
            )
        ):
            stream_status.addWidget(QLabel(title_text), 0, index)
            value = QLabel("—")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            stream_status.addWidget(value, 1, index)
            self.stream_labels[key] = value
        layout.addLayout(stream_status)

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
            ("asr", "First ASR Partial"),
            ("finalize", "Turn Finalization"),
            ("translation", "Translation First Token"),
            ("tts", "TTS First Audio"),
            ("playback", "Client First Playback"),
            ("total", "Turn Total"),
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
        self._mode_changed()

    def _show_startup_notice(self) -> None:
        if (
            self.config.app_mode == "real"
            and self.config.interpreter_mode in {"remote", "remote_stream"}
            and not self.config.ai_gateway_token
        ):
            self.error_text.setPlainText(
                "Remote Mode 尚未配置 AI_GATEWAY_TOKEN。GUI 可以启动，但停止录音后不会调用服务。"
            )
        elif (
            self.config.app_mode == "real"
            and self.config.interpreter_mode == "local"
            and not self.config.dashscope_api_key
        ):
            self.error_text.setPlainText("Local Direct 模式尚未配置 DASHSCOPE_API_KEY。")

    def _start_recording(self) -> None:
        if self._thread is not None:
            return
        if self.mode_combo.currentData() == "remote_stream":
            self._start_streaming()
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
        if isinstance(self._worker, StreamingWorker):
            self._worker.request_stop()
            self.status_label.setText("正在结束并刷新当前 Turn")
            self.stop_button.setEnabled(False)
            return
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
        self.latency_labels["finalize"].setText("—")
        self.latency_labels["translation"].setText(_format_ms(result.translation_latency_ms))
        self.latency_labels["tts"].setText(_format_ms(result.tts_latency_ms))
        self.latency_labels["playback"].setText("—")
        self.latency_labels["total"].setText(_format_ms(result.total_latency_ms))
        if result.generated_audio_path is not None:
            self._replace_latest_audio(result.generated_audio_path)

    def _start_streaming(self) -> None:
        try:
            stream_config = replace(
                self.config,
                interpreter_mode="remote_stream",
                stream_capture_mode=str(self.capture_combo.currentData()),
            )
            stream_config.validate_for_processing()
            self.player.stop()
            self.error_text.clear()
            self.source_text.clear()
            self.target_text.clear()
            self._reset_latencies()
            self._reset_stream_labels()
            self.stream_labels["connection"].setText("连接中")
            self.stream_labels["microphone"].setText("等待启动")
            self.status_label.setText("正在建立流式连接")
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.replay_button.setEnabled(False)
            worker = StreamingWorker(stream_config, self.pipeline, self.player)
            worker.event_received.connect(self._handle_stream_event)
            worker.audio_ready.connect(self._stream_audio_ready)
            worker.completed.connect(self._complete)
            worker.failed.connect(self._fail)
            self._start_worker(worker)
        except Exception as exc:
            self._fail(str(exc))

    def _handle_stream_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        if event_type == "session.started":
            self.stream_labels["connection"].setText("已连接")
            self.stream_labels["microphone"].setText("Listening")
            self.status_label.setText("Listening")
        elif event_type == "vad.speech_start":
            self.stream_labels["vad"].setText("Speech Detected")
            self.stream_labels["turn"].setText("采集中")
        elif event_type == "vad.speech_end":
            self.stream_labels["vad"].setText("Silence")
            self.stream_labels["turn"].setText("Finalizing Turn")
        elif event_type == "asr.partial":
            self.source_text.setPlainText(str(event.get("text", "")))
        elif event_type == "asr.final":
            self.source_text.setPlainText(str(event.get("text", "")))
            self.stream_labels["turn"].setText("ASR Final")
        elif event_type == "translation.partial":
            self.target_text.setPlainText(str(event.get("text", "")))
        elif event_type == "translation.final":
            self.target_text.setPlainText(str(event.get("text", "")))
            self.stream_labels["turn"].setText("Translation Final")
        elif event_type == "tts.audio.start":
            self.status_label.setText("Streaming Playback")
            capture_status = (
                "暂停（Safe）"
                if self.capture_combo.currentData() == "safe"
                else "持续（Headphones）"
            )
            self.stream_labels["microphone"].setText(capture_status)
        elif event_type == "tts.audio.end":
            self.stream_labels["microphone"].setText("Listening")
        elif event_type == "turn.completed":
            self._handle_completed_turn(event)
        elif event_type == "warning":
            if event.get("code") == "FALLBACK_REQUIRED":
                self.stream_labels["fallback"].setText("HTTP Fallback")
            self.error_text.setPlainText(str(event.get("message", "")))
        elif event_type == "session.completed":
            self.stream_labels["connection"].setText("已断开")

    def _handle_completed_turn(self, event: dict[str, Any]) -> None:
        self.stream_labels["turn"].setText("Completed")
        self.source_text.setPlainText(str(event.get("recognized_text", "")))
        self.target_text.setPlainText(str(event.get("translated_text", "")))
        if event.get("fallback") == "http":
            self.stream_labels["fallback"].setText("HTTP Fallback")
        metrics = event.get("metrics", {})
        if not isinstance(metrics, dict):
            return
        self.latency_labels["asr"].setText(
            _format_ms(float(metrics.get("asr_first_partial_ms", metrics.get("asr_ms", 0))))
        )
        self.latency_labels["finalize"].setText(
            _format_ms(float(metrics.get("turn_finalize_ms", 0)))
        )
        translation = metrics.get(
            "translation_first_token_ms", metrics.get("translation_ms", 0)
        )
        self.latency_labels["translation"].setText(_format_ms(float(translation)))
        self.latency_labels["tts"].setText(
            _format_ms(float(metrics.get("tts_first_audio_ms", metrics.get("tts_ms", 0))))
        )
        self.latency_labels["playback"].setText(
            _format_ms(float(metrics.get("client_first_playback_ms", 0)))
        )
        self.latency_labels["total"].setText(
            _format_ms(float(metrics.get("turn_total_ms", 0)))
        )

    def _stream_audio_ready(self, path: Path) -> None:
        self._replace_latest_audio(path)

    def _replace_latest_audio(self, path: Path) -> None:
        previous = self.latest_audio_path
        if (
            previous is not None
            and previous != path
            and previous.parent != path.parent
            and previous.parent.name.startswith("aivi-stream-playback-")
        ):
            shutil.rmtree(previous.parent, ignore_errors=True)
        self.latest_audio_path = path

    def _mode_changed(self) -> None:
        streaming = self.mode_combo.currentData() == "remote_stream"
        self.capture_combo.setEnabled(streaming)
        self.start_button.setText("开始同传" if streaming else "开始录音")
        self.stop_button.setText("停止同传" if streaming else "停止并翻译")

    def _reset_stream_labels(self) -> None:
        for label in self.stream_labels.values():
            label.setText("—")

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
        if isinstance(self._worker, StreamingWorker):
            self._worker.request_stop()
        if (
            self._thread is not None
            and self._thread.isRunning()
            and not self._thread.wait(3000)
        ):
            self.error_text.setPlainText("后台请求仍在结束中，请稍候再次关闭窗口。")
            event.ignore()
            return
        self.recorder.cleanup()
        cleanup = getattr(self.pipeline, "cleanup", None)
        if not callable(cleanup):
            cleanup = getattr(getattr(self.pipeline, "text_to_speech", None), "cleanup", None)
        if callable(cleanup):
            cleanup()
        if (
            self.latest_audio_path is not None
            and self.latest_audio_path.parent.name.startswith("aivi-stream-playback-")
        ):
            shutil.rmtree(self.latest_audio_path.parent, ignore_errors=True)
        event.accept()


def _format_ms(value: float) -> str:
    return f"{max(0.0, value):.0f} ms"
