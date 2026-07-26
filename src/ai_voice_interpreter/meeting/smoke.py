from __future__ import annotations

import argparse
import json
import queue
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np

from ..config import AppConfig
from ..remote.streaming_gateway_client import StreamPacket
from .audio_io import AudioIOMetrics
from .controller import MeetingBridgeController
from .devices import AudioDeviceInfo, AudioRouteProfile, ResolvedAudioRoute
from .route_guard import RouteGuard


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Bidirectional Meeting Bridge smoke")
    parser.add_argument("--local-chinese-audio", type=Path)
    parser.add_argument("--remote-english-audio", type=Path)
    parser.add_argument("--play-local-translation", action="store_true")
    parser.add_argument("--capture-virtual-mic", action="store_true")
    parser.add_argument("--keep-files", action="store_true")
    parser.add_argument("--standard-voice", action="store_true")
    parser.add_argument("--clone-local-once", action="store_true")
    parser.add_argument("--json-report", type=Path)
    parser.add_argument("--max-turns", type=int, default=3)
    parser.add_argument("--no-real-api", action="store_true")
    return parser


class FakeGatewayClient:
    def __init__(self, *, fallback: bool = False) -> None:
        self.session_id = ""
        self.request_id = ""
        self.direction = ""
        self.bridge_id = ""
        self.closed = False
        self.stopped = False
        self.sent_audio_bytes = 0
        self.fallback = fallback

    def open(self, **kwargs: Any) -> dict[str, Any]:
        self.direction = str(kwargs["session_role"])
        self.bridge_id = str(kwargs["bridge_id"])
        self.session_id = str(uuid.uuid4())
        self.request_id = str(uuid.uuid4())
        return {
            "type": "session.started",
            "session_id": self.session_id,
            "request_id": self.request_id,
            "protocol_version": "1.1",
            "bridge_id": self.bridge_id,
            "session_role": self.direction,
            "pipeline_provider": "livetranslate",
            "upstream_session_id": f"mock-upstream-{self.direction}",
            "audio_output": {
                "format": "pcm_s16le",
                "sample_rate": 24000,
                "channels": 1,
                "sample_width": 2,
            },
        }

    def send_audio(self, pcm: bytes) -> None:
        self.sent_audio_bytes += len(pcm)

    def send_ping(self) -> None:
        return None

    def stop_session(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.closed = True

    def packets(self, timeout: float | None = None) -> Iterator[StreamPacket]:
        del timeout
        source, translation = (
            (
                "你好，很高兴见到你。我们今天主要讨论项目进度和下一步的交付计划。",
                "Hello, it is nice to meet you. Today we will discuss project "
                "progress and the next delivery plan.",
            )
            if self.direction == "local_to_remote"
            else (
                "Hello, it is nice to meet you. Today, we need to discuss the "
                "project progress and the next delivery plan.",
                "你好，很高兴见到你。今天我们需要讨论项目进度和下一步交付计划。",
            )
        )
        turn_id = str(uuid.uuid4())
        if self.fallback:
            yield StreamPacket(
                event={"type": "provider.changed", "from": "livetranslate", "to": "modular"}
            )
        yield StreamPacket(event={"type": "asr.partial", "turn_id": turn_id, "text": source[:8]})
        yield StreamPacket(event={"type": "asr.final", "turn_id": turn_id, "text": source})
        yield StreamPacket(
            event={"type": "translation.partial", "turn_id": turn_id, "text": translation[:12]}
        )
        yield StreamPacket(
            event={"type": "translation.final", "turn_id": turn_id, "text": translation}
        )
        yield StreamPacket(
            event={
                "type": "tts.audio.start",
                "turn_id": turn_id,
                "sample_rate": 24000,
                "channels": 1,
                "sample_width": 2,
            }
        )
        pcm = (np.sin(np.arange(4800) / 11) * 2000).astype("<i2").tobytes()
        for offset in range(0, len(pcm), 960):
            yield StreamPacket(audio=pcm[offset : offset + 960])
        yield StreamPacket(event={"type": "tts.audio.end", "turn_id": turn_id})
        yield StreamPacket(
            event={
                "type": "turn.completed",
                "turn_id": turn_id,
                "upstream_response_id": f"mock-response-{self.direction}",
            }
        )
        yield StreamPacket(
            event={
                "type": "session.completed",
                "session_id": self.session_id,
                "bridge_id": self.bridge_id,
            }
        )


class FakeAudioIO:
    active_count = 0

    def __init__(self, input_device: AudioDeviceInfo, output_device: AudioDeviceInfo) -> None:
        self.input_device = input_device
        self.output_device = output_device
        self.metrics = AudioIOMetrics()
        self.output_bytes = 0
        self.active = False
        self._inputs: queue.Queue[bytes] = queue.Queue()
        self._inputs.put((np.zeros(1600, dtype="<i2")).tobytes())

    def start(self) -> None:
        self.active = True
        type(self).active_count += 1

    def read_input_pcm(self, timeout: float = 0.25) -> bytes:
        del timeout
        try:
            pcm = self._inputs.get_nowait()
            self.metrics.input_frames += round(
                len(pcm) // 2 * self.input_device.default_sample_rate / 16000
            )
            return pcm
        except queue.Empty:
            time.sleep(0.001)
            return b""

    def enqueue_output_pcm(self, pcm: bytes, *, input_rate: int = 24000) -> None:
        del input_rate
        self.output_bytes += len(pcm)
        self.metrics.output_frames += round(
            len(pcm) // 2 * self.output_device.default_sample_rate / 24000
        )
        if self.metrics.first_output_write_at == 0:
            self.metrics.first_output_write_at = time.monotonic()
        self.metrics.output_queue_peak = max(1, self.metrics.output_queue_peak)

    def close(self) -> None:
        if self.active:
            type(self).active_count -= 1
        self.active = False


def mock_route() -> ResolvedAudioRoute:
    return ResolvedAudioRoute(
        _device(1, "Physical Headset Mic", 1, 0, headphones=False),
        _device(2, "BlackHole 2ch", 2, 2, blackhole=2),
        _device(3, "BlackHole 16ch", 16, 16, blackhole=16),
        _device(4, "USB Headphones", 0, 2, headphones=True),
    )


def _device(
    index: int,
    name: str,
    inputs: int,
    outputs: int,
    *,
    blackhole: int | None = None,
    headphones: bool = False,
) -> AudioDeviceInfo:
    return AudioDeviceInfo(
        index=index,
        stable_key=f"mock-{index}",
        name=name,
        host_api="Core Audio",
        max_input_channels=inputs,
        max_output_channels=outputs,
        default_sample_rate=48000.0,
        is_virtual=blackhole is not None,
        is_blackhole=blackhole is not None,
        blackhole_channels=blackhole,
        is_headphones_candidate=headphones,
        is_microphone_candidate=bool(inputs and blackhole is None),
    )


def run_mock_bridge(
    *, fallback: bool = False, reconnect_direction: str | None = None
) -> dict[str, Any]:
    route = mock_route()
    profile = AudioRouteProfile(
        route.local_microphone.stable_key,
        route.meeting_virtual_microphone_output.stable_key,
        route.meeting_audio_capture_input.stable_key,
        route.local_headphones_output.stable_key,
        meeting_setup_confirmed=True,
    )
    clients: list[FakeGatewayClient] = []
    audio_ios: list[FakeAudioIO] = []

    def client_factory() -> FakeGatewayClient:
        client = FakeGatewayClient(fallback=fallback and not clients)
        clients.append(client)
        return client

    def audio_factory(input_device: Any, output_device: Any) -> FakeAudioIO:
        audio = FakeAudioIO(input_device, output_device)
        audio_ios.append(audio)
        return audio

    config = AppConfig(app_mode="mock", ai_gateway_token="mock-token")
    controller = MeetingBridgeController(
        config,
        route,
        profile,
        gateway_ready={
            "streaming": {
                "bridge_sessions_supported": True,
                "streaming_max_connections_per_token": 2,
            }
        },
        route_guard=RouteGuard(settings_check=lambda _device, _direction: True),
        client_factory=client_factory,
        audio_io_factory=audio_factory,
    )
    controller.start()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if all(session.metrics.turns for session in controller.sessions.values()):
            break
        time.sleep(0.005)
    if reconnect_direction:
        controller.reconnect(reconnect_direction)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if controller.sessions[reconnect_direction].metrics.turns:
                break
            time.sleep(0.005)
    snapshot = controller.snapshot()
    controller.stop()
    directions = snapshot["directions"]
    assert isinstance(directions, dict)
    local = directions["local_to_remote"]
    remote = directions["remote_to_local"]
    return {
        "success": (
            "project progress" in local["metrics"]["translation_final"].lower()
            and "项目进度" in remote["metrics"]["translation_final"]
            and audio_ios[0].output_bytes > 0
            and audio_ios[1].output_bytes > 0
        ),
        "mock": True,
        "paid_model_calls": 0,
        "bridge_id": controller.bridge_id,
        "local_to_remote": local,
        "remote_to_local": remote,
        "virtual_mic_output_bytes": next(
            item.output_bytes
            for item in audio_ios
            if item.output_device.blackhole_channels == 2
        ),
        "headphones_output_bytes": next(
            item.output_bytes for item in audio_ios if item.output_device.is_headphones_candidate
        ),
        "cross_route_detected": False,
        "duplicate_audio_detected": False,
        "reconnected_direction": reconnect_direction,
        "active_audio_streams_after_stop": FakeAudioIO.active_count,
    }


def main() -> int:
    args = _parser().parse_args()
    if not args.no_real_api:
        raise SystemExit(
            "真实 Meeting Bridge Smoke 必须先通过 meeting-doctor 和硬件 RouteGuard；"
            "当前命令未显式启用 --no-real-api。"
        )
    report = run_mock_bridge()
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_report:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
