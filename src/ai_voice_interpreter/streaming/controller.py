from __future__ import annotations

import logging
import queue
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..audio.player import MacAudioPlayer
from ..config import AppConfig
from ..exceptions import GatewayError, InterpreterError
from ..remote.streaming_gateway_client import StreamingGatewayClient, StreamPacket
from .microphone import StreamingMicrophone
from .player import PCMStreamingPlayer

logger = logging.getLogger(__name__)
EventCallback = Callable[[dict[str, Any]], None]
AudioReadyCallback = Callable[[Path], None]


class StreamingSessionController:
    """Owns one WSS session, capture loop, receive loop, playback and HTTP fallback."""

    def __init__(
        self,
        config: AppConfig,
        *,
        microphone: StreamingMicrophone | None = None,
        stream_player: PCMStreamingPlayer | None = None,
        client: StreamingGatewayClient | None = None,
        fallback_pipeline: Any | None = None,
        fallback_player: MacAudioPlayer | None = None,
    ) -> None:
        self.config = config
        self.microphone = microphone or StreamingMicrophone(
            sample_rate=config.audio_sample_rate,
            channels=config.audio_channels,
            chunk_ms=config.stream_audio_chunk_ms,
            queue_max_chunks=config.stream_send_queue_max_chunks,
            ring_buffer_seconds=config.stream_ring_buffer_seconds,
        )
        self.stream_player = stream_player or PCMStreamingPlayer(
            prebuffer_ms=config.stream_playback_prebuffer_ms,
            queue_max_seconds=config.stream_playback_queue_max_seconds,
            save_last_turn=config.stream_playback_save_last_turn,
        )
        self.client = client or StreamingGatewayClient(
            config.ai_gateway_base_url,
            config.ai_gateway_token,
            config.network_timeout_seconds,
        )
        self.fallback_pipeline = fallback_pipeline
        self.fallback_player = fallback_player
        self._stop = threading.Event()
        self._receiver: threading.Thread | None = None
        self._sender: threading.Thread | None = None
        self._receiver_error: Exception | None = None
        self._sender_error: Exception | None = None
        self._send_queue: queue.Queue[bytes | None] = queue.Queue(
            config.stream_send_queue_max_chunks
        )
        self._send_queue_peak = 0
        self._send_pressure_warned = False
        self._completed = threading.Event()
        self._on_event: EventCallback = lambda _event: None
        self._on_audio_ready: AudioReadyCallback = lambda _path: None
        self._last_ping = 0.0
        self._streamed_audio_started = False
        self._turn_started_at: dict[str, float] = {}
        self._turn_ended_at: dict[str, float] = {}

    def request_stop(self) -> None:
        self._stop.set()

    def run(
        self,
        *,
        on_event: EventCallback | None = None,
        on_audio_ready: AudioReadyCallback | None = None,
    ) -> None:
        self.config.validate_for_processing()
        self._on_event = on_event or self._on_event
        self._on_audio_ready = on_audio_ready or self._on_audio_ready
        self._stop.clear()
        self._completed.clear()
        self._receiver_error = None
        self._sender_error = None
        self._streamed_audio_started = False
        stream_opened = False
        try:
            started = self.client.open(
                source_language=self.config.source_language,
                target_language=self.config.target_language,
                voice=self.config.effective_tts_voice,
                chunk_ms=self.config.stream_audio_chunk_ms,
                voice_mode=self.config.stream_voice_mode,
            )
            stream_opened = True
            self._on_event(started)
            self.microphone.start()
            self._receiver = threading.Thread(target=self._receive_loop, daemon=True)
            self._sender = threading.Thread(target=self._network_send_loop, daemon=True)
            self._receiver.start()
            self._sender.start()
            self._capture_pump_loop()
            self.microphone.stop()
            if self._receiver_error is not None:
                self._cancel_sender()
                raise self._receiver_error
            self._finish_sender()
            if self._sender_error is not None:
                raise self._sender_error
            self.client.stop_session()
            self._receiver.join(timeout=self.config.network_timeout_seconds)
            if self._receiver.is_alive():
                raise GatewayError("等待流式服务结束超时。", self.client.request_id)
            if self._receiver_error is not None:
                raise self._receiver_error
            if not self._completed.is_set():
                raise GatewayError("流式服务未正常完成会话。", self.client.request_id)
        except Exception as exc:
            if self.microphone.is_running:
                self.microphone.stop()
            self._cancel_sender()
            if (
                stream_opened
                and self.config.stream_http_fallback
                and not self._streamed_audio_started
            ):
                self._on_event(
                    {
                        "type": "warning",
                        "code": "FALLBACK_REQUIRED",
                        "message": "流式连接失败，正在尝试 HTTPS 按句回退。",
                    }
                )
                if self._run_http_fallback():
                    return
            raise exc
        finally:
            if self._sender is not None and self._sender.is_alive():
                self._cancel_sender()
            self.client.close()

    def cleanup(self) -> None:
        self.client.close()
        self.stream_player.cleanup()

    def _capture_pump_loop(self) -> None:
        while (
            not self._stop.is_set()
            and self._receiver_error is None
            and self._sender_error is None
        ):
            chunk = self.microphone.read(timeout=0.25)
            if chunk:
                try:
                    self._send_queue.put_nowait(chunk)
                except queue.Full as exc:
                    raise GatewayError(
                        "WebSocket 发送队列已满，停止流式会话。", self.client.request_id
                    ) from exc
                depth = self._send_queue.qsize()
                self._send_queue_peak = max(self._send_queue_peak, depth)
                if not self._send_pressure_warned and depth / self._send_queue.maxsize >= 0.8:
                    self._send_pressure_warned = True
                    logger.warning(
                        "WebSocket send queue pressure depth=%d max=%d",
                        depth,
                        self._send_queue.maxsize,
                    )
            if self.microphone.dropped_chunks:
                raise GatewayError("采集发送队列已满，停止流式会话。", self.client.request_id)

    def _network_send_loop(self) -> None:
        self._last_ping = time.monotonic()
        try:
            while True:
                try:
                    chunk = self._send_queue.get(timeout=0.25)
                except queue.Empty:
                    if time.monotonic() - self._last_ping >= 10:
                        self.client.send_ping()
                        self._last_ping = time.monotonic()
                    continue
                if chunk is None:
                    return
                self.client.send_audio(chunk)
        except Exception as exc:
            self._sender_error = exc
            self._stop.set()

    def _finish_sender(self) -> None:
        sender = self._sender
        if sender is None:
            return
        try:
            self._send_queue.put(None, timeout=1)
        except queue.Full as exc:
            raise GatewayError("WebSocket 发送队列无法刷新。", self.client.request_id) from exc
        sender.join(timeout=5)
        if sender.is_alive():
            raise GatewayError("WebSocket 发送线程未能及时结束。", self.client.request_id)
        self._sender = None

    def _cancel_sender(self) -> None:
        sender = self._sender
        if sender is None:
            return
        while True:
            try:
                self._send_queue.get_nowait()
            except queue.Empty:
                break
        with suppress(queue.Full):
            self._send_queue.put_nowait(None)
        sender.join(timeout=5)
        self._sender = None

    def _receive_loop(self) -> None:
        try:
            for packet in self.client.packets(timeout=self.config.network_timeout_seconds):
                self._handle_packet(packet)
        except Exception as exc:
            self._receiver_error = exc
            self._stop.set()

    def _handle_packet(self, packet: StreamPacket) -> None:
        if packet.audio is not None:
            self._streamed_audio_started = True
            self.stream_player.feed(packet.audio)
            return
        assert packet.event is not None
        event = packet.event
        event_type = event.get("type")
        turn_id = str(event.get("turn_id", ""))
        if event_type == "vad.speech_start" and turn_id:
            speech_ms = float(event.get("speech_ms", 0))
            self._turn_started_at[turn_id] = time.monotonic() - speech_ms / 1000
        elif event_type == "vad.speech_end" and turn_id:
            silence_ms = float(event.get("silence_ms", 0))
            self._turn_ended_at[turn_id] = time.monotonic() - silence_ms / 1000
        elif event_type == "tts.audio.start":
            if self.config.stream_capture_mode == "safe" and self.microphone.is_running:
                self.microphone.stop()
                self.microphone.clear_pending()
            self.stream_player.start_turn(
                sample_rate=int(event["sample_rate"]),
                channels=int(event["channels"]),
                sample_width=int(event["sample_width"]),
            )
        elif event_type == "tts.audio.end":
            path = self.stream_player.stop_turn()
            if path is not None:
                self._on_audio_ready(path)
            if self.config.stream_capture_mode == "safe" and not self._stop.is_set():
                self.microphone.start()
        elif event_type == "session.completed":
            event["client_queue_peaks"] = {
                "capture": self.microphone.peak_queue_depth,
                "send": self._send_queue_peak,
                "playback": self.stream_player.peak_queue_depth,
            }
            self._completed.set()
        elif event_type == "turn.completed":
            metrics = event.get("metrics")
            if isinstance(metrics, dict):
                if turn_id in self._turn_ended_at and self.stream_player.first_playback_at:
                    metrics["client_first_playback_ms"] = (
                        self.stream_player.first_playback_at - self._turn_ended_at.pop(turn_id)
                    ) * 1000
                if turn_id in self._turn_started_at and self.stream_player.first_playback_at:
                    metrics["end_to_end_ttfa_ms"] = (
                        self.stream_player.first_playback_at - self._turn_started_at.pop(turn_id)
                    ) * 1000
        self._on_event(event)

    def _run_http_fallback(self) -> bool:
        if self.fallback_pipeline is None or self.fallback_player is None:
            return False
        try:
            with tempfile.TemporaryDirectory(prefix="aivi-fallback-") as directory:
                audio_path = self.microphone.write_ring_wav(Path(directory) / "fallback.wav")
                result = self.fallback_pipeline.process(audio_path)
                if result.error or result.generated_audio_path is None:
                    raise InterpreterError(result.error or "HTTP 回退未返回音频。")
                self._on_event(
                    {
                        "type": "turn.completed",
                        "fallback": "http",
                        "recognized_text": result.recognized_text,
                        "translated_text": result.translated_text,
                        "metrics": {
                            "turn_total_ms": result.total_latency_ms,
                            "asr_ms": result.asr_latency_ms,
                            "translation_ms": result.translation_latency_ms,
                            "tts_ms": result.tts_latency_ms,
                        },
                    }
                )
                self._on_audio_ready(result.generated_audio_path)
                self.fallback_player.play(result.generated_audio_path)
            return True
        except Exception:
            logger.exception("HTTP fallback failed")
            return False
