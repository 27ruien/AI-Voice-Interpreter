from __future__ import annotations

from dataclasses import replace

import pytest

from ai_voice_interpreter.meeting.route_guard import RouteGuard
from ai_voice_interpreter.meeting.smoke import mock_route


def validate(route=None, *, token=True, capacity=2, supported=True, confirmed=True):  # type: ignore[no-untyped-def]
    return RouteGuard(settings_check=lambda _device, _direction: True).validate(
        route or mock_route(),
        gateway_token_configured=token,
        gateway_ready={
            "streaming": {
                "bridge_sessions_supported": supported,
                "streaming_max_connections_per_token": capacity,
            }
        },
        meeting_setup_confirmed=confirmed,
    )


def test_correct_four_device_topology_passes() -> None:
    assert validate().can_start


@pytest.mark.parametrize(
    "field,replacement_field,expected",
    [
        ("local_microphone", "meeting_virtual_microphone_output", "MIC_IS_PHYSICAL"),
        ("local_headphones_output", "meeting_virtual_microphone_output", "HEADPHONES_ARE_PHYSICAL"),
        (
            "meeting_virtual_microphone_output",
            "meeting_audio_capture_input",
            "VIRTUAL_MIC_IS_BLACKHOLE_2CH",
        ),
        (
            "meeting_audio_capture_input",
            "meeting_virtual_microphone_output",
            "MEETING_CAPTURE_IS_BLACKHOLE_16CH",
        ),
    ],
)
def test_wrong_role_devices_fail(field: str, replacement_field: str, expected: str) -> None:
    route = mock_route()
    broken = replace(route, **{field: getattr(route, replacement_field)})
    result = validate(broken)
    assert not result.can_start
    assert expected in {check.code for check in result.failures}


def test_same_input_and_same_output_are_rejected() -> None:
    route = mock_route()
    same_input = replace(route, meeting_audio_capture_input=route.local_microphone)
    same_output = replace(route, local_headphones_output=route.meeting_virtual_microphone_output)
    assert "LOOP_INPUTS_DISTINCT" in {c.code for c in validate(same_input).failures}
    assert "LOOP_OUTPUTS_DISTINCT" in {c.code for c in validate(same_output).failures}


@pytest.mark.parametrize(
    "kwargs,code",
    [
        ({"token": False}, "GATEWAY_TOKEN"),
        ({"capacity": 1}, "GATEWAY_TWO_SESSIONS"),
        ({"supported": False}, "GATEWAY_BRIDGE_SUPPORT"),
        ({"confirmed": False}, "MEETING_SETUP_CONFIRMED"),
    ],
)
def test_gateway_and_setup_prerequisites(kwargs: dict[str, object], code: str) -> None:
    result = validate(**kwargs)
    assert code in {check.code for check in result.failures}


def test_unsupported_sample_rate_or_device_open_fails() -> None:
    result = RouteGuard(settings_check=lambda _device, _direction: False).validate(
        mock_route(),
        gateway_token_configured=True,
        gateway_ready={
            "streaming": {
                "bridge_sessions_supported": True,
                "streaming_max_connections_per_token": 2,
            }
        },
        meeting_setup_confirmed=True,
    )
    assert "DEVICE_OPEN_CAPABILITY" in {check.code for check in result.failures}
