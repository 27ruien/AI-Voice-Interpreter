from __future__ import annotations

import argparse
import json
import logging
import os
import threading
import time
import wave
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .config import AppConfig
from .exceptions import GatewayError
from .logging_config import configure_logging
from .remote.streaming_gateway_client import StreamingGatewayClient, StreamPacket
from .streaming.microphone import StreamingMicrophone
from .streaming.player import PCMStreamingPlayer

logger = logging.getLogger(__name__)


class _NullOutput:
    def __enter__(self) -> _NullOutput:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def write(self, _pcm: bytes) -> None:
        return None


@dataclass(slots=True)
class SmokeReport:
    success: bool = False
    session_id: str = ""
    turn_ids: list[str] = field(default_factory=list)
    asr_partial_count: int = 0
    asr_final_count: int = 0
    translation_partial_count: int = 0
    translation_final_count: int = 0
    tts_audio_chunks: int = 0
    tts_audio_bytes: int = 0
    first_asr_partial_ms: float = 0.0
    turn_finalize_ms: float = 0.0
    translation_first_token_ms: float = 0.0
    tts_first_audio_ms: float = 0.0
    client_first_playback_ms: float = 0.0
    end_to_end_ttfa_ms: float = 0.0
    queue_peaks: dict[str, int] = field(default_factory=dict)
    fallback: bool = False
    provider_request_ids: dict[str, str] = field(default_factory=dict)
    final_text_lengths: dict[str, int] = field(default_factory=dict)
    asr_final: str = ""
    translation_final: str = ""
    error_code: str = ""
    error: str = ""
    output_audio_paths: list[str] = field(default_factory=list)
    pipeline_provider: str = ""
    upstream_model: str = ""
    upstream_session_id: str = ""
    upstream_response_ids: list[str] = field(default_factory=list)
    last_event_id: str = ""
    source_transcription_unavailable: bool = False
    voice_clone_status: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    first_source_transcript_ms: float = 0.0
    first_translation_ms: float = 0.0
    first_audio_ms: float = 0.0
    speech_end_to_first_playback_ms: float = 0.0
    session_total_ms: float = 0.0


class StreamingSmokeRunner:
    def __init__(
        self,
        config: AppConfig,
        *,
        play: bool = False,
        keep_files: bool = False,
        pipeline_provider: str | None = None,
        source_transcription_enabled: bool = True,
    ) -> None:
        self.config = config
        self.keep_files = keep_files
        self.pipeline_provider = pipeline_provider
        self.source_transcription_enabled = source_transcription_enabled
        output_factory = None if play else lambda **_kwargs: _NullOutput()
        self.player = PCMStreamingPlayer(
            prebuffer_ms=config.stream_playback_prebuffer_ms,
            queue_max_seconds=config.stream_playback_queue_max_seconds,
            save_last_turn=True,
            output_factory=output_factory,
        )
        self.client = StreamingGatewayClient(
            config.ai_gateway_base_url,
            config.ai_gateway_token,
            config.network_timeout_seconds,
        )
        self.report = SmokeReport()
        self._receiver_error: Exception | None = None
        self._completed = threading.Event()
        self._speech_started_at = 0.0
        self._speech_ended_at = 0.0
        self._microphone: StreamingMicrophone | None = None
        self._session_started_at = 0.0

    def run_file(self, audio_path: Path) -> SmokeReport:
        with wave.open(str(audio_path), "rb") as source:
            if (
                source.getframerate() != 16000
                or source.getnchannels() != 1
                or source.getsampwidth() != 2
            ):
                raise GatewayError("测试 WAV 必须是 16 kHz、单声道、16-bit PCM。")
            started = self.client.open(
                source_language=self.config.source_language,
                target_language=self.config.target_language,
                voice=self.config.effective_tts_voice,
                chunk_ms=self.config.stream_audio_chunk_ms,
                voice_mode=self.config.stream_voice_mode,
                pipeline_provider=self.pipeline_provider,
                source_transcription_enabled=self.source_transcription_enabled,
            )
            self._record_started(started)
            self.report.session_id = str(started["session_id"])
            receiver = threading.Thread(target=self._receive_loop, daemon=True)
            receiver.start()
            frames = 16000 * self.config.stream_audio_chunk_ms // 1000
            deadline = time.monotonic()
            while chunk := source.readframes(frames):
                self._send_audio_or_raise(chunk, receiver)
                deadline += self.config.stream_audio_chunk_ms / 1000
                time.sleep(max(0.0, deadline - time.monotonic()))
            self._speech_ended_at = time.monotonic()
            for _ in range(max(1, 1000 // self.config.stream_audio_chunk_ms)):
                self._send_audio_or_raise(b"\0\0" * frames, receiver)
                deadline += self.config.stream_audio_chunk_ms / 1000
                time.sleep(max(0.0, deadline - time.monotonic()))
            self.client.stop_session()
            receiver.join(timeout=self.config.network_timeout_seconds)
            if receiver.is_alive():
                raise GatewayError("流式 Smoke 等待服务结束超时。")
            if self._receiver_error is not None:
                raise self._receiver_error
            self.report.success = self._successful_output()
            if not self.report.success:
                raise GatewayError("流式 Smoke 未产生完整 Turn。")
            return self.report

    def run_microphone(self, duration_seconds: float) -> SmokeReport:
        microphone = StreamingMicrophone(
            chunk_ms=self.config.stream_audio_chunk_ms,
            queue_max_chunks=self.config.stream_send_queue_max_chunks,
            ring_buffer_seconds=self.config.stream_ring_buffer_seconds,
        )
        self._microphone = microphone
        started = self.client.open(
            source_language=self.config.source_language,
            target_language=self.config.target_language,
            voice=self.config.effective_tts_voice,
            chunk_ms=self.config.stream_audio_chunk_ms,
            voice_mode=self.config.stream_voice_mode,
            pipeline_provider=self.pipeline_provider,
            source_transcription_enabled=self.source_transcription_enabled,
        )
        self._record_started(started)
        self.report.session_id = str(started["session_id"])
        receiver = threading.Thread(target=self._receive_loop, daemon=True)
        microphone.start()
        receiver.start()
        try:
            deadline = time.monotonic() + duration_seconds
            while time.monotonic() < deadline and self._receiver_error is None:
                chunk = microphone.read(0.25)
                if chunk:
                    self._send_audio_or_raise(chunk, receiver)
                if microphone.dropped_chunks:
                    raise GatewayError("麦克风发送队列溢出。")
        except KeyboardInterrupt:
            logger.info("Microphone smoke interrupted by user")
        finally:
            if microphone.is_running:
                microphone.stop()
            self.client.stop_session()
        receiver.join(timeout=self.config.network_timeout_seconds)
        if microphone.is_running:
            microphone.stop()
        if self._receiver_error is not None:
            raise self._receiver_error
        self.report.success = self._successful_output()
        return self.report

    def close(self) -> None:
        self.client.close()
        if not self.keep_files:
            self.player.cleanup()
            self.report.output_audio_paths.clear()

    def _receive_loop(self) -> None:
        try:
            for packet in self.client.packets(timeout=self.config.network_timeout_seconds):
                self._handle_packet(packet)
        except Exception as exc:
            self._receiver_error = exc

    def _send_audio_or_raise(self, pcm: bytes, receiver: threading.Thread) -> None:
        if self._receiver_error is not None:
            raise self._receiver_error
        try:
            self.client.send_audio(pcm)
        except Exception as send_error:
            # The receive worker may already have decoded the server's structured
            # error while the send side observes only the subsequent close frame.
            receiver.join(timeout=min(0.25, self.config.network_timeout_seconds))
            if self._receiver_error is not None:
                raise self._receiver_error from send_error
            raise

    def _handle_packet(self, packet: StreamPacket) -> None:
        now = time.monotonic()
        if packet.audio is not None:
            self.report.tts_audio_chunks += 1
            self.report.tts_audio_bytes += len(packet.audio)
            self.player.feed(packet.audio)
            return
        assert packet.event is not None
        event = packet.event
        event_type = event["type"]
        if event_type == "session.started":
            self._record_started(event)
        elif event_type == "vad.speech_start":
            self._speech_started_at = now - float(event.get("speech_ms", 0)) / 1000
            self.report.turn_ids.append(str(event["turn_id"]))
        elif event_type == "vad.speech_end":
            self._speech_ended_at = now - float(event.get("silence_ms", 0)) / 1000
        elif event_type == "asr.partial":
            self.report.asr_partial_count += 1
            if self.report.first_source_transcript_ms == 0 and self._speech_ended_at:
                self.report.first_source_transcript_ms = max(
                    0.0, (now - self._speech_ended_at) * 1000
                )
        elif event_type == "asr.final":
            self.report.asr_final_count += 1
            self.report.asr_final = str(event.get("text", ""))
            self.report.final_text_lengths["asr"] = len(self.report.asr_final)
        elif event_type == "translation.partial":
            self.report.translation_partial_count += 1
            if self.report.first_translation_ms == 0 and self._speech_ended_at:
                self.report.first_translation_ms = max(
                    0.0, (now - self._speech_ended_at) * 1000
                )
        elif event_type == "translation.final":
            self.report.translation_final_count += 1
            self.report.translation_final = str(event.get("text", ""))
            self.report.final_text_lengths["translation"] = len(
                self.report.translation_final
            )
        elif event_type == "tts.audio.start":
            if self.report.first_audio_ms == 0 and self._speech_ended_at:
                self.report.first_audio_ms = max(
                    0.0, (now - self._speech_ended_at) * 1000
                )
            if (
                self.config.stream_capture_mode == "safe"
                and self._microphone is not None
                and self._microphone.is_running
            ):
                self._microphone.stop()
                self._microphone.clear_pending()
            self.player.start_turn(
                sample_rate=int(event["sample_rate"]),
                channels=int(event["channels"]),
                sample_width=int(event["sample_width"]),
            )
        elif event_type == "tts.audio.end":
            path = self.player.stop_turn()
            if path is not None:
                self.report.output_audio_paths = [str(path)]
            if (
                self.config.stream_capture_mode == "safe"
                and self._microphone is not None
                and not self._microphone.is_running
            ):
                self._microphone.start()
        elif event_type == "turn.completed":
            metrics = event.get("metrics", {})
            if isinstance(metrics, dict):
                self.report.first_asr_partial_ms = float(metrics.get("asr_first_partial_ms", 0))
                self.report.turn_finalize_ms = float(metrics.get("turn_finalize_ms", 0))
                self.report.translation_first_token_ms = float(
                    metrics.get("translation_first_token_ms", 0)
                )
                self.report.tts_first_audio_ms = float(metrics.get("tts_first_audio_ms", 0))
            request_ids = event.get("provider_request_ids", {})
            if isinstance(request_ids, dict):
                self.report.provider_request_ids = {
                    str(key): str(value) for key, value in request_ids.items() if value
                }
            if self.player.first_playback_at and self._speech_ended_at:
                self.report.client_first_playback_ms = (
                    self.player.first_playback_at - self._speech_ended_at
                ) * 1000
                self.report.speech_end_to_first_playback_ms = (
                    self.report.client_first_playback_ms
                )
            if self.player.first_playback_at and self._speech_started_at:
                self.report.end_to_end_ttfa_ms = (
                    self.player.first_playback_at - self._speech_started_at
                ) * 1000
            response_id = str(event.get("upstream_response_id", ""))
            if response_id and response_id not in self.report.upstream_response_ids:
                self.report.upstream_response_ids.append(response_id)
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.report.usage = usage
            self.report.last_event_id = str(event.get("event_id", ""))
        elif event_type == "usage.updated":
            usage = event.get("usage")
            if isinstance(usage, dict):
                self.report.usage = usage
        elif event_type == "source_transcription.unavailable":
            self.report.source_transcription_unavailable = True
        elif event_type == "voice_clone.status":
            self.report.voice_clone_status = str(event.get("status", ""))
        elif event_type == "session.completed":
            peaks = event.get("queue_peaks", {})
            if isinstance(peaks, dict):
                self.report.queue_peaks = {str(key): int(value) for key, value in peaks.items()}
            self.report.queue_peaks["playback"] = self.player.peak_queue_depth
            self.report.upstream_session_id = str(
                event.get("upstream_session_id", self.report.upstream_session_id)
            )
            self.report.last_event_id = str(
                event.get("last_event_id", self.report.last_event_id)
            )
            if self._session_started_at:
                self.report.session_total_ms = (now - self._session_started_at) * 1000
            self._completed.set()

    def _record_started(self, event: dict[str, Any]) -> None:
        if not self._session_started_at:
            self._session_started_at = time.monotonic()
        self.report.session_id = str(event.get("session_id", self.report.session_id))
        self.report.pipeline_provider = str(
            event.get("pipeline_provider", self.report.pipeline_provider)
        )
        self.report.upstream_model = str(
            event.get("upstream_model", self.report.upstream_model)
        )
        self.report.upstream_session_id = str(
            event.get("upstream_session_id", self.report.upstream_session_id)
        )

    def _successful_output(self) -> bool:
        source_ok = (
            self.report.asr_final_count > 0
            or self.report.source_transcription_unavailable
            or not self.source_transcription_enabled
        )
        return (
            self._completed.is_set()
            and source_ok
            and self.report.translation_final_count > 0
            and self.report.tts_audio_chunks > 0
            and self.report.tts_audio_bytes > 0
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI Voice Interpreter WSS streaming smoke")
    parser.add_argument("--base-url")
    parser.add_argument("--token")
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--microphone", action="store_true")
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument("--play", action="store_true")
    parser.add_argument("--keep-files", action="store_true")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--safe-mode", action="store_true")
    parser.add_argument("--headphones-mode", action="store_true")
    parser.add_argument("--max-turns", type=int, default=5)
    parser.add_argument("--json-report", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    overrides = dict(os.environ)
    if args.base_url:
        overrides["AI_GATEWAY_BASE_URL"] = args.base_url
    if args.token:
        overrides["AI_GATEWAY_TOKEN"] = args.token
    config = AppConfig.load(environ=overrides)
    if args.safe_mode and args.headphones_mode:
        raise SystemExit("--safe-mode 与 --headphones-mode 不能同时使用。")
    if args.safe_mode or args.headphones_mode:
        config = replace(
            config,
            stream_capture_mode="safe" if args.safe_mode else "headphones",
        )
    configure_logging(config.log_level)
    if bool(args.audio) == bool(args.microphone):
        raise SystemExit("必须且只能选择 --audio 或 --microphone。")
    if args.repeat < 1 or args.max_turns < 1:
        raise SystemExit("--repeat 和 --max-turns 必须大于 0。")
    reports: list[dict[str, Any]] = []
    exit_code = 0
    for _ in range(args.repeat):
        runner = StreamingSmokeRunner(config, play=args.play, keep_files=args.keep_files)
        try:
            report = (
                runner.run_microphone(args.duration)
                if args.microphone
                else runner.run_file(args.audio)
            )
            if len(report.turn_ids) > args.max_turns:
                raise GatewayError("Smoke 产生的 Turn 超过 --max-turns 限制。")
        except Exception as exc:
            report = runner.report
            report.error = str(exc)
            report.error_code = type(exc).__name__
            exit_code = 1
        finally:
            runner.close()
        reports.append(asdict(report))
    payload: dict[str, Any] = {"runs": reports, "success": exit_code == 0}
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_report:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(rendered + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
