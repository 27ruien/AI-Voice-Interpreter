from __future__ import annotations

import logging
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QThread
from PySide6.QtGui import QCloseEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..audio.player import MacAudioPlayer
from ..audio.recorder import MicrophoneRecorder
from ..config import AppConfig
from ..exceptions import InterpreterError
from ..meeting.controller import MeetingBridgeController
from ..meeting.devices import AudioDeviceCatalog, AudioRouteProfile
from ..meeting.doctor import gateway_readyz
from ..meeting.route_guard import RouteGuard
from ..models import PipelineResult, ProcessingStatus
from .workers import (
    MeetingAudioCheckWorker,
    MeetingBridgeWorker,
    PlaybackWorker,
    ProcessingWorker,
    StreamingWorker,
)

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
        self._meeting_catalog: AudioDeviceCatalog | None = None
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
        subtitle = QLabel("实时语音翻译 MVP · 中文 → English")
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
        self.mode_combo.addItem("会议桥接", "meeting_bridge")
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
        mode_row.addWidget(QLabel("输出音色"))
        self.voice_combo = QComboBox()
        self.voice_combo.addItem("标准音色", "standard")
        self.voice_combo.addItem("模仿我的音色（实验）", "clone_once")
        self.voice_combo.setCurrentIndex(
            self.voice_combo.findData(self.config.stream_voice_mode)
        )
        mode_row.addWidget(self.voice_combo)
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
                ("provider", "Pipeline"),
            )
        ):
            stream_status.addWidget(QLabel(title_text), 0, index)
            value = QLabel("—")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            stream_status.addWidget(value, 1, index)
            self.stream_labels[key] = value
        layout.addLayout(stream_status)

        self.meeting_frame = self._build_meeting_frame()
        layout.addWidget(self.meeting_frame)

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
        scroll = QScrollArea()
        scroll.setObjectName("main_scroll_area")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(root)
        self.setCentralWidget(scroll)
        self._refresh_meeting_devices()
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
        if self.mode_combo.currentData() == "meeting_bridge":
            self._start_meeting_bridge()
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
        if isinstance(self._worker, MeetingBridgeWorker):
            self._worker.request_stop()
            self.status_label.setText("STOPPING")
            self.stop_button.setEnabled(False)
            return
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
        if self.mode_combo.currentData() == "meeting_bridge":
            self.start_button.setEnabled(True)
            self.stop_button.setEnabled(False)
            self._update_meeting_setup_actions()

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
                stream_voice_mode=str(self.voice_combo.currentData()),
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
            if stream_config.stream_voice_mode == "clone_once":
                self.error_text.setPlainText(
                    "实验音色只可用于本人或已获授权的声音；会话开始阶段可能先使用默认音色过渡。"
                )
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
            provider = str(event.get("pipeline_provider", ""))
            self.stream_labels["provider"].setText(
                "实时翻译（推荐）" if provider == "livetranslate" else "模块化回退"
            )
            self.status_label.setText("Listening")
        elif event_type == "provider.changed":
            self.stream_labels["provider"].setText("模块化回退")
            self.stream_labels["fallback"].setText("模块化回退")
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
        elif event_type == "source_transcription.unavailable":
            self.error_text.setPlainText("源语言字幕暂不可用，翻译与音频将继续。")
        elif event_type == "voice_clone.status" and event.get("status") == "failed":
            self.error_text.setPlainText("声音复刻失败，请切换为标准音色。")
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
        mode = self.mode_combo.currentData()
        streaming = mode == "remote_stream"
        meeting = mode == "meeting_bridge"
        self.capture_combo.setEnabled(streaming)
        self.voice_combo.setEnabled(streaming)
        self.meeting_frame.setVisible(meeting)
        self.start_button.setText(
            "Start Meeting Bridge" if meeting else "开始同传" if streaming else "开始录音"
        )
        self.stop_button.setText(
            "Stop Meeting Bridge" if meeting else "停止同传" if streaming else "停止并翻译"
        )
        self.replay_button.setEnabled(not meeting and self.latest_audio_path is not None)

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
        if isinstance(self._worker, (StreamingWorker, MeetingBridgeWorker)):
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

    def _build_meeting_frame(self) -> QGroupBox:
        frame = QGroupBox("Meeting Bridge Setup")
        frame.setObjectName("meeting_bridge_setup")
        layout = QVBoxLayout(frame)
        warning = QLabel(
            "会议桥接模式必须使用耳机。外放可能导致中文译音重新进入物理麦克风；"
            "当前版本没有声学回声消除。"
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("background:#fff7ed;color:#9a3412;padding:8px;")
        layout.addWidget(warning)
        grid = QGridLayout()
        self.meeting_device_combos: dict[str, QComboBox] = {}
        roles = (
            ("local_microphone", "Local Microphone"),
            (
                "meeting_virtual_microphone_output",
                "Meeting Virtual Microphone (BlackHole 2ch)",
            ),
            (
                "meeting_audio_capture_input",
                "Meeting Audio Capture (BlackHole 16ch)",
            ),
            ("local_headphones_output", "Local Headphones"),
        )
        for row, (key, label) in enumerate(roles):
            grid.addWidget(QLabel(label), row, 0)
            combo = QComboBox()
            combo.setObjectName(f"meeting_{key}")
            combo.currentIndexChanged.connect(self._update_meeting_setup_actions)
            grid.addWidget(combo, row, 1)
            self.meeting_device_combos[key] = combo
        layout.addLayout(grid)
        actions = QHBoxLayout()
        self.meeting_refresh_button = QPushButton("Refresh Devices")
        self.meeting_refresh_button.setObjectName("meeting_refresh_devices")
        self.meeting_audio_check_button = QPushButton("Run Audio Check")
        self.meeting_audio_check_button.setObjectName("meeting_run_audio_check")
        self.meeting_save_button = QPushButton("Save Route Profile")
        self.meeting_save_button.setObjectName("meeting_save_route_profile")
        guide = QPushButton("打开会议设置说明")
        copy = QPushButton("复制设置摘要")
        self.meeting_refresh_button.clicked.connect(self._refresh_meeting_devices)
        self.meeting_audio_check_button.clicked.connect(
            self._run_meeting_audio_check
        )
        self.meeting_save_button.clicked.connect(self._save_meeting_routes)
        guide.clicked.connect(self._show_meeting_guide)
        copy.clicked.connect(self._copy_meeting_summary)
        for button in (
            self.meeting_refresh_button,
            self.meeting_audio_check_button,
            self.meeting_save_button,
            guide,
            copy,
        ):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.meeting_setup_confirmed = QCheckBox("已完成会议软件设置")
        layout.addWidget(self.meeting_setup_confirmed)
        self.meeting_route_status = QLabel("No Audio Route Profile saved")
        self.meeting_route_status.setObjectName("meeting_route_profile_status")
        self.meeting_route_status.setWordWrap(True)
        layout.addWidget(self.meeting_route_status)
        self.meeting_global_label = QLabel("Meeting Bridge：UNCONFIGURED")
        layout.addWidget(self.meeting_global_label)
        cards = QGridLayout()
        self.meeting_direction_labels: dict[str, dict[str, QLabel]] = {}
        for column, (direction, title) in enumerate(
            (
                ("local_to_remote", "我说中文 → 对方听英文"),
                ("remote_to_local", "对方说英文 → 我听中文"),
            )
        ):
            card = QFrame()
            card.setFrameShape(QFrame.Shape.StyledPanel)
            card_layout = QVBoxLayout(card)
            card_layout.addWidget(QLabel(title))
            labels: dict[str, QLabel] = {}
            for key, initial in (
                ("state", "Disconnected"),
                ("route", "输入 / 输出：—"),
                ("level", "输入电平：Idle"),
                ("source_partial", "源字幕 Partial：—"),
                ("source_final", "源字幕 Final：—"),
                ("translation_partial", "翻译 Partial：—"),
                ("translation_final", "翻译 Final：—"),
                ("latency", "首译音：—"),
                ("voice", "音色：—"),
                ("provider", "Provider：—"),
                ("fallback", "Fallback：否"),
                ("error", "错误：—"),
            ):
                value = QLabel(initial)
                value.setWordWrap(True)
                card_layout.addWidget(value)
                labels[key] = value
            self.meeting_direction_labels[direction] = labels
            cards.addWidget(card, 0, column)
        layout.addLayout(cards)
        frame.setVisible(False)
        return frame

    def _refresh_meeting_devices(self) -> None:
        try:
            catalog = AudioDeviceCatalog.discover()
            self._meeting_catalog = catalog
            profile = AudioRouteProfile.load()
            selected = {
                key: getattr(profile, key) if profile else ""
                for key in self.meeting_device_combos
            }
            groups = {
                "local_microphone": catalog.microphones(),
                "meeting_virtual_microphone_output": catalog.blackhole(2),
                "meeting_audio_capture_input": catalog.blackhole(16),
                "local_headphones_output": catalog.headphones(),
            }
            for key, combo in self.meeting_device_combos.items():
                combo.clear()
                combo.addItem("请选择", "")
                for device in groups[key]:
                    combo.addItem(device.safe_name, device.stable_key)
                index = combo.findData(selected[key])
                combo.setCurrentIndex(max(0, index))
            if profile:
                self.meeting_setup_confirmed.setChecked(profile.meeting_setup_confirmed)
                if profile.resolve(catalog) is not None:
                    self.meeting_route_status.setText("Saved Audio Route Profile loaded")
                else:
                    self.meeting_route_status.setText(
                        "Saved Audio Route Profile cannot be resolved; select devices again"
                    )
            else:
                self.meeting_setup_confirmed.setChecked(False)
                self.meeting_route_status.setText("No Audio Route Profile saved")
            self._update_meeting_setup_actions()
        except Exception as exc:
            self.error_text.setPlainText(f"刷新会议音频设备失败：{exc}")
            self._update_meeting_setup_actions()

    def _update_meeting_setup_actions(self) -> None:
        complete = bool(self.meeting_device_combos) and all(
            combo.currentData() for combo in self.meeting_device_combos.values()
        )
        idle = self._thread is None
        self.meeting_save_button.setEnabled(complete and idle)
        self.meeting_audio_check_button.setEnabled(complete and idle)

    def _selected_meeting_profile(self) -> AudioRouteProfile:
        values = {
            key: str(combo.currentData() or "")
            for key, combo in self.meeting_device_combos.items()
        }
        if not all(values.values()):
            raise InterpreterError("必须明确选择四个 Meeting Bridge 音频端点。")
        return AudioRouteProfile(
            **values,
            meeting_setup_confirmed=self.meeting_setup_confirmed.isChecked(),
        )

    def _save_meeting_routes(self) -> None:
        try:
            catalog = self._meeting_catalog or AudioDeviceCatalog.discover()
            profile = self._selected_meeting_profile()
            route = profile.resolve(catalog)
            if route is None:
                raise InterpreterError("选择的音频设备无法重新解析，请刷新后重试。")
            ready = gateway_readyz(self.config)
            guard = RouteGuard().validate(
                route,
                gateway_token_configured=bool(self.config.ai_gateway_token),
                gateway_ready=ready,
                meeting_setup_confirmed=profile.meeting_setup_confirmed,
            )
            if not guard.can_start:
                reasons = "\n".join(
                    f"{check.code}: {check.message}" for check in guard.failures
                )
                self.meeting_route_status.setText("Audio Route Profile not saved")
                self.error_text.setPlainText(f"RouteGuard failed:\n{reasons}")
                return
            path = profile.save()
            self.meeting_route_status.setText("Meeting audio route saved")
            self.error_text.setPlainText(
                f"Meeting audio route saved\n{path}"
            )
        except Exception as exc:
            self.meeting_route_status.setText("Audio Route Profile not saved")
            self.error_text.setPlainText(str(exc))

    def _run_meeting_audio_check(self) -> None:
        try:
            profile = self._selected_meeting_profile()
            catalog = self._meeting_catalog or AudioDeviceCatalog.discover()
            if profile.resolve(catalog) is None:
                raise InterpreterError("选择的音频设备无法重新解析，请刷新后重试。")
            self.error_text.setPlainText("Running local meeting audio checks…")
            self.meeting_save_button.setEnabled(False)
            self.meeting_audio_check_button.setEnabled(False)
            worker = MeetingAudioCheckWorker(catalog, profile)
            worker.report_ready.connect(self._display_meeting_audio_check)
            worker.failed.connect(self._fail)
            self._start_worker(worker)
        except Exception as exc:
            self.error_text.setPlainText(str(exc))

    def _display_meeting_audio_check(self, report: dict[str, Any]) -> None:
        tests = report.get("tests", {})
        lines = [
            "PASS Local audio checks"
            if report.get("can_start_meeting_bridge")
            else "FAIL Local audio checks"
        ]
        if isinstance(tests, dict):
            for name, result in tests.items():
                if not isinstance(result, dict):
                    continue
                message = str(result.get("message", "completed"))
                lines.append(f"{result.get('status', 'WARN')} {name}: {message}")
        self.error_text.setPlainText("\n".join(lines))

    def _start_meeting_bridge(self) -> None:
        try:
            if self._thread is not None:
                return
            catalog = self._meeting_catalog or AudioDeviceCatalog.discover()
            selected_profile = self._selected_meeting_profile()
            profile = AudioRouteProfile.load()
            if profile is None:
                raise InterpreterError("请先点击 Save Route Profile 保存并验证音频路由。")
            if profile != selected_profile:
                raise InterpreterError("设备选择已更改，请先重新保存 Route Profile。")
            route = profile.resolve(catalog)
            if route is None:
                raise InterpreterError("保存的设备无法重新解析，请刷新并重新选择。")
            ready = gateway_readyz(self.config)
            if ready is None:
                raise InterpreterError("Gateway readyz 不可访问。")
            controller = MeetingBridgeController(
                self.config,
                route,
                profile,
                gateway_ready=ready,
            )
            worker = MeetingBridgeWorker(controller)
            worker.event_received.connect(self._handle_meeting_event)
            worker.snapshot_changed.connect(self._handle_meeting_snapshot)
            worker.state_changed.connect(self._meeting_state_changed)
            worker.completed.connect(lambda: self._meeting_state_changed("STOPPED"))
            worker.failed.connect(self._fail)
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(True)
            self.replay_button.setEnabled(False)
            self._meeting_state_changed("STARTING")
            self._start_worker(worker)
        except Exception as exc:
            self._fail(str(exc))

    def _handle_meeting_event(self, direction: str, event: dict[str, Any]) -> None:
        labels = self.meeting_direction_labels[direction]
        event_type = str(event.get("type", ""))
        if event_type == "session.started":
            labels["state"].setText("Connected / Listening")
            labels["provider"].setText(
                f"Provider：{event.get('pipeline_provider', 'unknown')}"
            )
            self.meeting_global_label.setText(
                f"Meeting Bridge：RUNNING · bridge_id={event.get('bridge_id', '—')}"
            )
        elif event_type == "asr.partial":
            labels["source_partial"].setText(
                f"源字幕 Partial：{event.get('text', '')}"
            )
        elif event_type == "asr.final":
            labels["source_final"].setText(f"源字幕 Final：{event.get('text', '')}")
        elif event_type == "translation.partial":
            labels["translation_partial"].setText(
                f"翻译 Partial：{event.get('text', '')}"
            )
        elif event_type == "translation.final":
            labels["translation_final"].setText(
                f"翻译 Final：{event.get('text', '')}"
            )
        elif event_type == "provider.changed":
            labels["fallback"].setText("Fallback：Modular")
        elif event_type == "error":
            labels["error"].setText(f"错误：{event.get('message', '')}")
            labels["state"].setText("Failed")

    def _handle_meeting_snapshot(self, snapshot: dict[str, Any]) -> None:
        directions = snapshot.get("directions", {})
        if not isinstance(directions, dict):
            return
        for direction, details in directions.items():
            if direction not in self.meeting_direction_labels or not isinstance(
                details, dict
            ):
                continue
            labels = self.meeting_direction_labels[direction]
            metrics = details.get("metrics", {})
            if not isinstance(metrics, dict):
                metrics = {}
            labels["state"].setText(str(details.get("state", "Disconnected")))
            labels["route"].setText(
                f"输入：{details.get('input_device', '—')} / "
                f"输出：{details.get('output_device', '—')}"
            )
            labels["level"].setText(
                f"输入电平 RMS：{float(metrics.get('rms', 0.0)):.4f}"
            )
            labels["latency"].setText(
                f"首译音：{float(metrics.get('first_output_write_ms', 0.0)):.0f} ms"
            )
            labels["voice"].setText(
                f"音色：{details.get('voice', '—')} ({details.get('voice_mode', '—')})"
            )

    def _meeting_state_changed(self, state: str) -> None:
        self.status_label.setText(state)
        if not self.meeting_global_label.text().startswith("Meeting Bridge：RUNNING ·"):
            self.meeting_global_label.setText(f"Meeting Bridge：{state}")
        if state == "DEGRADED":
            self.meeting_global_label.setText("Meeting Bridge：DEGRADED")
        elif state in {"STOPPED", "FAILED"}:
            self.meeting_global_label.setText(f"Meeting Bridge：{state}")
            for labels in self.meeting_direction_labels.values():
                labels["state"].setText("Disconnected")
                labels["level"].setText("输入电平：Idle")

    def _show_meeting_guide(self) -> None:
        QMessageBox.information(
            self,
            "会议软件设置",
            "Zoom / Teams / 浏览器会议：\n\n"
            "Microphone：BlackHole 2ch\n"
            "Speaker：BlackHole 16ch\n\n"
            "不要修改 macOS 系统默认设备；必须佩戴耳机。",
        )

    def _copy_meeting_summary(self) -> None:
        QApplication.clipboard().setText(
            "Meeting Microphone: BlackHole 2ch\nMeeting Speaker: BlackHole 16ch"
        )
        self.error_text.setPlainText("会议设置摘要已复制，不包含 Token 或密钥。")


def _format_ms(value: float) -> str:
    return f"{max(0.0, value):.0f} ms"
