from __future__ import annotations

import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from ..config import AppConfig
from ..exceptions import GatewayError
from ..remote.streaming_gateway_client import StreamingGatewayClient
from ..streaming.protocol import MEETING_PROTOCOL_VERSION
from .audio_io import DirectionalAudioIO
from .devices import AudioRouteProfile, ResolvedAudioRoute
from .route_guard import RouteGuard, RouteGuardResult

logger = logging.getLogger(__name__)


class BridgeState(StrEnum):
    UNCONFIGURED = "UNCONFIGURED"
    READY = "READY"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    DEGRADED = "DEGRADED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class DirectionState(StrEnum):
    DISCONNECTED = "DISCONNECTED"
    CONNECTING = "CONNECTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(slots=True)
class DirectionMetrics:
    first_source_partial_ms: float = 0.0
    first_translation_ms: float = 0.0
    first_audio_ms: float = 0.0
    first_output_write_ms: float = 0.0
    turns: int = 0
    audio_chunks: int = 0
    audio_bytes: int = 0
    fallback: bool = False
    provider: str = ""
    gateway_session_id: str = ""
    upstream_session_id: str = ""
    upstream_response_ids: list[str] = field(default_factory=list)
    last_error_code: str = ""
    source_partial: str = ""
    source_final: str = ""
    translation_partial: str = ""
    translation_final: str = ""


EventCallback = Callable[[str, dict[str, Any]], None]
FailureCallback = Callable[[str, Exception], None]


class DirectionalBridgeSession:
    def __init__(
        self,
        *,
        direction: str,
        bridge_id: str,
        source_language: str,
        target_language: str,
        voice_mode: str,
        voice: str,
        audio_io: DirectionalAudioIO,
        gateway_client: StreamingGatewayClient,
        chunk_ms: int,
        event_callback: EventCallback | None = None,
        failure_callback: FailureCallback | None = None,
    ) -> None:
        self.direction = direction
        self.bridge_id = bridge_id
        self.source_language = source_language
        self.target_language = target_language
        self.voice_mode = voice_mode
        self.voice = voice
        self.audio_io = audio_io
        self.gateway_client = gateway_client
        self.chunk_ms = chunk_ms
        self.event_callback = event_callback or (lambda _direction, _event: None)
        self.failure_callback = failure_callback or (lambda _direction, _error: None)
        self.state = DirectionState.DISCONNECTED
        self.metrics = DirectionMetrics()
        self.started_at = 0.0
        self.error: Exception | None = None
        self._stop = threading.Event()
        self._sender: threading.Thread | None = None
        self._receiver: threading.Thread | None = None
        self._output_rate = 24000

    def open_gateway(self) -> dict[str, Any]:
        self.state = DirectionState.CONNECTING
        started = self.gateway_client.open(
            source_language=self.source_language,
            target_language=self.target_language,
            voice=self.voice,
            chunk_ms=self.chunk_ms,
            voice_mode=self.voice_mode,
            protocol_version=MEETING_PROTOCOL_VERSION,
            bridge_id=self.bridge_id,
            session_role=self.direction,
            mode="meeting_bridge",
        )
        self.started_at = time.monotonic()
        self._handle_event(started)
        return started

    def start_audio(self) -> None:
        if self.state != DirectionState.CONNECTING:
            raise RuntimeError("方向 Session 尚未完成 Gateway 握手。")
        self.audio_io.start()
        self._stop.clear()
        self._sender = threading.Thread(
            target=self._send_loop,
            name=f"meeting-send-{self.direction}",
            daemon=True,
        )
        self._receiver = threading.Thread(
            target=self._receive_loop,
            name=f"meeting-recv-{self.direction}",
            daemon=True,
        )
        self.state = DirectionState.RUNNING
        self._receiver.start()
        self._sender.start()

    def stop(self, timeout: float = 20.0) -> None:
        if self.state in {DirectionState.STOPPED, DirectionState.DISCONNECTED}:
            self.gateway_client.close()
            self.audio_io.close()
            self.state = DirectionState.STOPPED
            return
        self.state = DirectionState.STOPPING
        self._stop.set()
        try:
            self.gateway_client.stop_session()
        except Exception:
            logger.debug("Unable to send meeting session.stop", exc_info=True)
        for thread in (self._sender, self._receiver):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=timeout)
        self.audio_io.close()
        self.gateway_client.close()
        self._sender = None
        self._receiver = None
        if self.state != DirectionState.FAILED:
            self.state = DirectionState.STOPPED

    def snapshot(self) -> dict[str, Any]:
        if (
            self.metrics.first_output_write_ms == 0
            and self.audio_io.metrics.first_output_write_at
            and self.started_at
        ):
            self.metrics.first_output_write_ms = max(
                0.0,
                (self.audio_io.metrics.first_output_write_at - self.started_at) * 1000,
            )
        return {
            "direction": self.direction,
            "state": self.state.value,
            "source_language": self.source_language,
            "target_language": self.target_language,
            "voice": self.voice,
            "voice_mode": self.voice_mode,
            "input_device": self.audio_io.input_device.safe_name,
            "output_device": self.audio_io.output_device.safe_name,
            "metrics": {
                **asdict(self.metrics),
                "input_queue_peak": self.audio_io.metrics.input_queue_peak,
                "output_queue_peak": self.audio_io.metrics.output_queue_peak,
                "underrun_count": self.audio_io.metrics.underrun_count,
                "rms": self.audio_io.metrics.rms,
                "peak": self.audio_io.metrics.peak,
                "backpressure_count": (
                    self.audio_io.metrics.input_backpressure_count
                    + self.audio_io.metrics.output_backpressure_count
                ),
                "input_seconds": round(
                    self.audio_io.metrics.input_frames
                    / self.audio_io.input_device.default_sample_rate,
                    3,
                ),
                "output_seconds": round(
                    self.audio_io.metrics.output_frames
                    / self.audio_io.output_device.default_sample_rate,
                    3,
                ),
            },
        }

    def _send_loop(self) -> None:
        last_ping = time.monotonic()
        try:
            while not self._stop.is_set():
                pcm = self.audio_io.read_input_pcm(0.25)
                if pcm:
                    self.gateway_client.send_audio(pcm)
                elif time.monotonic() - last_ping >= 10:
                    self.gateway_client.send_ping()
                    last_ping = time.monotonic()
        except Exception as exc:
            self._fail(exc)

    def _receive_loop(self) -> None:
        try:
            for packet in self.gateway_client.packets():
                if packet.audio is not None:
                    if self.metrics.first_audio_ms == 0:
                        self.metrics.first_audio_ms = self._elapsed_ms()
                    self.audio_io.enqueue_output_pcm(packet.audio, input_rate=self._output_rate)
                    self.metrics.audio_chunks += 1
                    self.metrics.audio_bytes += len(packet.audio)
                    continue
                assert packet.event is not None
                self._handle_event(packet.event)
                if packet.event.get("type") == "session.completed":
                    self._stop.set()
                    return
        except Exception as exc:
            if not self._stop.is_set():
                self._fail(exc)

    def _handle_event(self, event: dict[str, Any]) -> None:
        event_type = str(event.get("type", ""))
        if event_type == "session.started":
            self.metrics.gateway_session_id = str(event.get("session_id", ""))
            self.metrics.upstream_session_id = str(event.get("upstream_session_id", ""))
            self.metrics.provider = str(event.get("pipeline_provider", ""))
            output = event.get("audio_output")
            if isinstance(output, dict):
                self._output_rate = int(output.get("sample_rate", 24000))
            logger.info(
                "Meeting direction connected bridge_id=%s role=%s session_id=%s "
                "upstream_session_id=%s provider=%s",
                self.bridge_id,
                self.direction,
                self.metrics.gateway_session_id,
                self.metrics.upstream_session_id or "unavailable",
                self.metrics.provider,
            )
        elif event_type == "provider.changed":
            self.metrics.fallback = True
            self.metrics.provider = str(event.get("to", "modular"))
            logger.warning(
                "Meeting direction fallback bridge_id=%s role=%s provider=%s",
                self.bridge_id,
                self.direction,
                self.metrics.provider,
            )
        elif event_type == "asr.partial":
            if self.metrics.first_source_partial_ms == 0:
                self.metrics.first_source_partial_ms = self._elapsed_ms()
            self.metrics.source_partial = str(event.get("text", ""))
        elif event_type == "asr.final":
            self.metrics.source_final = str(event.get("text", ""))
        elif event_type == "translation.partial":
            if self.metrics.first_translation_ms == 0:
                self.metrics.first_translation_ms = self._elapsed_ms()
            self.metrics.translation_partial = str(event.get("text", ""))
        elif event_type == "translation.final":
            self.metrics.translation_final = str(event.get("text", ""))
        elif event_type == "turn.completed":
            self.metrics.turns += 1
            response_id = str(event.get("upstream_response_id", ""))
            if response_id and response_id not in self.metrics.upstream_response_ids:
                self.metrics.upstream_response_ids.append(response_id)
            logger.info(
                "Meeting turn completed bridge_id=%s role=%s turn=%d "
                "source_chars=%d translation_chars=%d audio_bytes=%d",
                self.bridge_id,
                self.direction,
                self.metrics.turns,
                len(self.metrics.source_final),
                len(self.metrics.translation_final),
                self.metrics.audio_bytes,
            )
        self.event_callback(self.direction, event)

    def _elapsed_ms(self) -> float:
        return max(0.0, (time.monotonic() - self.started_at) * 1000)

    def _fail(self, error: Exception) -> None:
        self.error = error
        self.metrics.last_error_code = getattr(error, "code", type(error).__name__)
        self.state = DirectionState.FAILED
        self._stop.set()
        self.audio_io.close()
        self.gateway_client.close()
        logger.error(
            "Meeting direction failed bridge_id=%s role=%s code=%s",
            self.bridge_id,
            self.direction,
            self.metrics.last_error_code,
        )
        self.failure_callback(self.direction, error)


class MeetingBridgeController:
    def __init__(
        self,
        config: AppConfig,
        route: ResolvedAudioRoute,
        profile: AudioRouteProfile,
        *,
        gateway_ready: dict[str, object],
        route_guard: RouteGuard | None = None,
        client_factory: Callable[[], StreamingGatewayClient] | None = None,
        audio_io_factory: Callable[[Any, Any], DirectionalAudioIO] | None = None,
        event_callback: EventCallback | None = None,
    ) -> None:
        self.config = config
        self.route = route
        self.profile = profile
        self.gateway_ready = gateway_ready
        self.route_guard = route_guard or RouteGuard()
        self.client_factory = client_factory or (
            lambda: StreamingGatewayClient(
                config.ai_gateway_base_url,
                config.ai_gateway_token,
                config.network_timeout_seconds,
            )
        )
        self.audio_io_factory = audio_io_factory or (
            lambda input_device, output_device: DirectionalAudioIO(
                input_device,
                output_device,
                queue_max_chunks=config.stream_send_queue_max_chunks,
            )
        )
        self.event_callback = event_callback
        self.bridge_id = ""
        self.state = BridgeState.READY
        self.sessions: dict[str, DirectionalBridgeSession] = {}
        self.started_at = 0.0
        self.stopped_at = 0.0
        self.guard_result: RouteGuardResult | None = None
        self._lock = threading.RLock()

    def start(self) -> None:
        with self._lock:
            if self.state in {BridgeState.STARTING, BridgeState.RUNNING}:
                raise RuntimeError("Meeting Bridge 已经运行。")
            self.guard_result = self.route_guard.validate(
                self.route,
                gateway_token_configured=bool(self.config.ai_gateway_token),
                gateway_ready=self.gateway_ready,
                meeting_setup_confirmed=self.profile.meeting_setup_confirmed,
            )
            if not self.guard_result.can_start:
                self.state = BridgeState.UNCONFIGURED
                detail = "；".join(check.message for check in self.guard_result.failures)
                raise GatewayError(f"Meeting RouteGuard 未通过：{detail}")
            self.state = BridgeState.STARTING
            self.bridge_id = str(uuid.uuid4())
            self.started_at = time.monotonic()
            self.stopped_at = 0.0
            self.sessions = self._build_sessions()
        opened: list[DirectionalBridgeSession] = []
        try:
            for direction in ("remote_to_local", "local_to_remote"):
                session = self.sessions[direction]
                session.open_gateway()
                opened.append(session)
            for direction in ("remote_to_local", "local_to_remote"):
                self.sessions[direction].start_audio()
            self._refresh_state()
            logger.info(
                "Meeting Bridge started bridge_id=%s routes=%s",
                self.bridge_id,
                self.route.safe_map(),
            )
        except Exception:
            for session in opened:
                session.stop()
            self.state = BridgeState.FAILED
            raise

    def stop(self) -> None:
        with self._lock:
            if self.state in {BridgeState.STOPPED, BridgeState.READY}:
                self.state = BridgeState.STOPPED
                return
            self.state = BridgeState.STOPPING
        for direction in ("local_to_remote", "remote_to_local"):
            session = self.sessions.get(direction)
            if session is not None:
                session.stop()
        self.stopped_at = time.monotonic()
        self.state = BridgeState.STOPPED
        logger.info(
            "Meeting Bridge stopped bridge_id=%s active_directions=0",
            self.bridge_id,
        )

    def reconnect(self, direction: str) -> None:
        if direction not in self.sessions:
            raise ValueError("未知 Meeting Bridge 方向。")
        previous = self.sessions[direction]
        previous.stop()
        replacement = self._new_session(direction)
        replacement.open_gateway()
        replacement.start_audio()
        self.sessions[direction] = replacement
        self._refresh_state()

    def snapshot(self) -> dict[str, Any]:
        directions = {
            direction: session.snapshot()
            for direction, session in self.sessions.items()
        }
        metrics = [item["metrics"] for item in directions.values()]
        return {
            "bridge_id": self.bridge_id,
            "state": self.state.value,
            "start_time_monotonic": self.started_at,
            "stop_time_monotonic": self.stopped_at,
            "active_directions": sum(
                session.state == DirectionState.RUNNING for session in self.sessions.values()
            ),
            "total_input_seconds": round(
                sum(float(item["input_seconds"]) for item in metrics), 3
            ),
            "total_output_seconds": round(
                sum(float(item["output_seconds"]) for item in metrics), 3
            ),
            "fallback_count": sum(bool(item["fallback"]) for item in metrics),
            "error_count": sum(bool(item["last_error_code"]) for item in metrics),
            "routes": self.route.safe_map(),
            "directions": directions,
        }

    def _build_sessions(self) -> dict[str, DirectionalBridgeSession]:
        return {
            direction: self._new_session(direction)
            for direction in ("local_to_remote", "remote_to_local")
        }

    def _new_session(self, direction: str) -> DirectionalBridgeSession:
        if direction == "local_to_remote":
            source, target = "zh", "en"
            input_device = self.route.local_microphone
            output_device = self.route.meeting_virtual_microphone_output
            voice = self.profile.local_to_remote_voice
            voice_mode = "standard"
        else:
            source, target = "en", "zh"
            input_device = self.route.meeting_audio_capture_input
            output_device = self.route.local_headphones_output
            voice = self.profile.remote_to_local_voice
            voice_mode = "standard"
        return DirectionalBridgeSession(
            direction=direction,
            bridge_id=self.bridge_id,
            source_language=source,
            target_language=target,
            voice_mode=voice_mode,
            voice=voice,
            audio_io=self.audio_io_factory(input_device, output_device),
            gateway_client=self.client_factory(),
            chunk_ms=self.config.stream_audio_chunk_ms,
            event_callback=self.event_callback,
            failure_callback=self._direction_failed,
        )

    def _direction_failed(self, _direction: str, _error: Exception) -> None:
        self._refresh_state()

    def _refresh_state(self) -> None:
        running = sum(
            session.state == DirectionState.RUNNING for session in self.sessions.values()
        )
        failed = sum(
            session.state == DirectionState.FAILED for session in self.sessions.values()
        )
        if running and failed:
            self.state = BridgeState.DEGRADED
        elif failed == 2:
            self.state = BridgeState.FAILED
            threading.Thread(target=self.stop, daemon=True).start()
        elif running == 2:
            self.state = BridgeState.RUNNING
