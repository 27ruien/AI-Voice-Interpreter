from __future__ import annotations

import time
from typing import Any

import pytest

from ai_voice_interpreter.config import AppConfig
from ai_voice_interpreter.meeting.controller import (
    BridgeState,
    MeetingBridgeController,
)
from ai_voice_interpreter.meeting.devices import AudioRouteProfile
from ai_voice_interpreter.meeting.route_guard import RouteGuard
from ai_voice_interpreter.meeting.smoke import (
    FakeAudioIO,
    FakeGatewayClient,
    mock_route,
    run_mock_bridge,
)


def profile() -> AudioRouteProfile:
    route = mock_route()
    return AudioRouteProfile(
        route.local_microphone.stable_key,
        route.meeting_virtual_microphone_output.stable_key,
        route.meeting_audio_capture_input.stable_key,
        route.local_headphones_output.stable_key,
        meeting_setup_confirmed=True,
    )


def ready() -> dict[str, object]:
    return {
        "streaming": {
            "bridge_sessions_supported": True,
            "streaming_max_connections_per_token": 2,
        }
    }


def test_mock_bridge_is_bidirectional_and_audio_isolated() -> None:
    report = run_mock_bridge()
    assert report["success"]
    assert report["paid_model_calls"] == 0
    assert report["virtual_mic_output_bytes"] > 0
    assert report["headphones_output_bytes"] > 0
    assert not report["cross_route_detected"]
    assert not report["duplicate_audio_detected"]
    assert report["active_audio_streams_after_stop"] == 0
    assert report["local_to_remote"]["output_device"].startswith("BlackHole 2ch")
    assert report["remote_to_local"]["input_device"].startswith("BlackHole 16ch")
    assert report["remote_to_local"]["output_device"].startswith("USB Headphones")


def test_one_direction_modular_fallback_does_not_stop_other_direction() -> None:
    report = run_mock_bridge(fallback=True)
    assert report["success"]
    fallbacks = {
        direction: report[direction]["metrics"]["fallback"]
        for direction in ("local_to_remote", "remote_to_local")
    }
    assert sum(fallbacks.values()) == 1
    assert all(report[direction]["metrics"]["turns"] == 1 for direction in fallbacks)


def test_guard_failure_opens_neither_audio_nor_gateway() -> None:
    calls = {"client": 0, "audio": 0}

    def client_factory() -> FakeGatewayClient:
        calls["client"] += 1
        return FakeGatewayClient()

    def audio_factory(_input: Any, _output: Any) -> FakeAudioIO:
        calls["audio"] += 1
        return FakeAudioIO(_input, _output)

    controller = MeetingBridgeController(
        AppConfig(app_mode="mock", ai_gateway_token=""),
        mock_route(),
        profile(),
        gateway_ready=ready(),
        route_guard=RouteGuard(settings_check=lambda _device, _direction: True),
        client_factory=client_factory,
        audio_io_factory=audio_factory,
    )
    with pytest.raises(Exception, match="RouteGuard"):
        controller.start()
    assert calls == {"client": 0, "audio": 0}
    assert controller.state == BridgeState.UNCONFIGURED


class FailingGateway(FakeGatewayClient):
    def packets(self, timeout: float | None = None):  # type: ignore[no-untyped-def]
        del timeout
        raise RuntimeError("direction disconnected")
        yield


def test_single_direction_failure_becomes_degraded_and_stop_cleans_streams() -> None:
    clients: list[FakeGatewayClient] = []

    def client_factory() -> FakeGatewayClient:
        client = FailingGateway() if not clients else FakeGatewayClient()
        clients.append(client)
        return client

    controller = MeetingBridgeController(
        AppConfig(app_mode="mock", ai_gateway_token="token"),
        mock_route(),
        profile(),
        gateway_ready=ready(),
        route_guard=RouteGuard(settings_check=lambda _device, _direction: True),
        client_factory=client_factory,
        audio_io_factory=FakeAudioIO,
    )
    controller.start()
    deadline = time.monotonic() + 1
    while time.monotonic() < deadline and controller.state == BridgeState.RUNNING:
        time.sleep(0.005)
    assert controller.state == BridgeState.DEGRADED
    controller.stop()
    assert controller.state == BridgeState.STOPPED
    assert FakeAudioIO.active_count == 0


def test_reconnect_replaces_only_requested_direction() -> None:
    clients: list[FakeGatewayClient] = []

    def client_factory() -> FakeGatewayClient:
        client = FakeGatewayClient()
        clients.append(client)
        return client

    controller = MeetingBridgeController(
        AppConfig(app_mode="mock", ai_gateway_token="token"),
        mock_route(),
        profile(),
        gateway_ready=ready(),
        route_guard=RouteGuard(settings_check=lambda _device, _direction: True),
        client_factory=client_factory,
        audio_io_factory=FakeAudioIO,
    )
    controller.start()
    untouched = controller.sessions["remote_to_local"]
    previous = controller.sessions["local_to_remote"]
    controller.reconnect("local_to_remote")
    assert controller.sessions["local_to_remote"] is not previous
    assert controller.sessions["remote_to_local"] is untouched
    controller.stop()
    assert FakeAudioIO.active_count == 0
