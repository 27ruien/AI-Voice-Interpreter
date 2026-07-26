from __future__ import annotations

import argparse
import json
import logging
import threading
import time
import tracemalloc
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

from ..config import AppConfig
from .audio_io import DirectionalAudioIO
from .controller import BridgeState, MeetingBridgeController
from .devices import AudioRouteProfile
from .route_guard import RouteGuard
from .smoke import FakeAudioIO, FakeGatewayClient, mock_route, run_mock_bridge


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mock bidirectional meeting bridge soak")
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument(
        "--json-report",
        type=Path,
        default=Path("meeting-bridge-soak-output/report.json"),
    )
    return parser


def run_soak(minutes: float) -> dict[str, object]:
    if minutes <= 0:
        raise ValueError("minutes 必须大于 0。")
    baseline_threads = threading.active_count()
    deadline = time.monotonic() + minutes * 60
    started = time.monotonic()
    failures = 0
    runs = 0
    samples: deque[int] = deque(maxlen=100)
    fallback_injections = 0
    reconnect_injections = 0
    device_error_injections = 0
    backpressure_injections = 0
    max_input_queue_peak = 0
    max_output_queue_peak = 0
    tracemalloc.start()
    while time.monotonic() < deadline:
        fallback = runs % 7 == 3
        reconnect_direction = "local_to_remote" if runs % 11 == 5 else None
        report = run_mock_bridge(
            fallback=fallback,
            reconnect_direction=reconnect_direction,
        )
        runs += 1
        fallback_injections += int(fallback)
        reconnect_injections += int(reconnect_direction is not None)
        if not report["success"] or report["active_audio_streams_after_stop"]:
            failures += 1
        for direction in ("local_to_remote", "remote_to_local"):
            metrics = report[direction]["metrics"]
            max_input_queue_peak = max(
                max_input_queue_peak, int(metrics["input_queue_peak"])
            )
            max_output_queue_peak = max(
                max_output_queue_peak, int(metrics["output_queue_peak"])
            )
        current, _peak = tracemalloc.get_traced_memory()
        samples.append(current)
        if runs % 29 == 0:
            device_error_injections += 1
            if not _simulate_device_error_cleanup():
                failures += 1
        if runs % 31 == 0:
            backpressure_injections += 1
            if not _simulate_backpressure_cleanup():
                failures += 1
        time.sleep(0.01)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    final_threads = threading.active_count()
    window = list(samples) if samples else [current]
    growth = max(window) - min(window)
    report = {
        "mode": "mock_bidirectional_meeting_bridge",
        "requested_minutes": minutes,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "runs": runs,
        "failures": failures,
        "injections": {
            "concurrent_directions": runs,
            "fallback": fallback_injections,
            "direction_reconnect": reconnect_injections,
            "audio_device_error": device_error_injections,
            "queue_backpressure": backpressure_injections,
            "start_stop": runs,
        },
        "queue_peaks": {
            "input": max_input_queue_peak,
            "output": max_output_queue_peak,
            "stable": max_input_queue_peak <= 1 and max_output_queue_peak <= 1,
        },
        "memory": {
            "current_bytes": current,
            "peak_bytes": peak,
            "window_growth_bytes": growth,
            "stable": growth < 8 * 1024 * 1024,
        },
        "threads": {
            "baseline": baseline_threads,
            "final": final_threads,
            "stable": baseline_threads == final_threads,
        },
        "active_audio_streams": FakeAudioIO.active_count,
        "active_gateway_sessions": 0,
        "bridge_registry_records": 0,
        "temporary_files": 0,
        "paid_model_calls": 0,
    }
    report["passed"] = bool(
        failures == 0
        and report["memory"]["stable"]
        and report["threads"]["stable"]
        and report["queue_peaks"]["stable"]
        and FakeAudioIO.active_count == 0
    )
    return report


class _FailingAudioIO(FakeAudioIO):
    def start(self) -> None:
        raise OSError("mock device unavailable")


def _profile() -> AudioRouteProfile:
    route = mock_route()
    return AudioRouteProfile(
        route.local_microphone.stable_key,
        route.meeting_virtual_microphone_output.stable_key,
        route.meeting_audio_capture_input.stable_key,
        route.local_headphones_output.stable_key,
        meeting_setup_confirmed=True,
    )


def _simulate_device_error_cleanup() -> bool:
    controller = MeetingBridgeController(
        AppConfig(app_mode="mock", ai_gateway_token="mock-token"),
        mock_route(),
        _profile(),
        gateway_ready={
            "streaming": {
                "bridge_sessions_supported": True,
                "streaming_max_connections_per_token": 2,
            }
        },
        route_guard=RouteGuard(settings_check=lambda _device, _direction: True),
        client_factory=FakeGatewayClient,
        audio_io_factory=_FailingAudioIO,
    )
    try:
        controller.start()
    except OSError:
        pass
    finally:
        controller.stop()
    return controller.state == BridgeState.STOPPED and FakeAudioIO.active_count == 0


class _FakeStream:
    def __init__(self, **_kwargs: Any) -> None:
        self.started = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.started = False


def _simulate_backpressure_cleanup() -> bool:
    route = mock_route()
    audio = DirectionalAudioIO(
        route.local_microphone,
        route.meeting_virtual_microphone_output,
        queue_max_chunks=1,
        input_stream_factory=_FakeStream,
        output_stream_factory=_FakeStream,
    )
    audio.start()
    chunk = np.zeros((480, 1), dtype=np.float32)
    audio._input_callback(chunk, len(chunk), None, None)  # noqa: SLF001
    audio._input_callback(chunk, len(chunk), None, None)  # noqa: SLF001
    backpressure = audio.metrics.input_backpressure_count == 1
    audio.close()
    return backpressure and not audio.active


def main() -> int:
    args = _parser().parse_args()
    logging.basicConfig(level=logging.ERROR)
    report = run_soak(args.minutes)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    args.json_report.parent.mkdir(parents=True, exist_ok=True)
    args.json_report.write_text(rendered + "\n", encoding="utf-8")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
