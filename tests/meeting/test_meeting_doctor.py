from __future__ import annotations

from ai_voice_interpreter.config import AppConfig
from ai_voice_interpreter.meeting.audio_doctor import run_audio_checks
from ai_voice_interpreter.meeting.devices import AudioDeviceCatalog
from ai_voice_interpreter.meeting.doctor import collect_checks, format_report


class NeverTouchAudio:
    def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
        raise AssertionError(f"audio API must not be called: {name}")


def test_meeting_doctor_reports_prerequisites_without_model_call() -> None:
    checks, ready = collect_checks(
        AppConfig(app_mode="real", ai_gateway_token=""),
        AudioDeviceCatalog([]),
        profile=None,
        gateway_payload={
            "streaming": {
                "bridge_sessions_supported": False,
                "streaming_max_connections_per_token": 1,
            }
        },
    )
    rendered = format_report(checks, ready)
    assert not ready
    assert "FAIL BlackHole 2ch" in rendered
    assert "FAIL BlackHole 16ch" in rendered
    assert "FAIL Token" in rendered
    assert "不会调用收费模型" in rendered


def test_audio_doctor_missing_route_opens_no_device_and_makes_no_paid_call() -> None:
    report = run_audio_checks(
        catalog=AudioDeviceCatalog([]),
        profile=None,
        sounddevice_module=NeverTouchAudio(),
    )
    assert report["paid_model_calls"] == 0
    assert report["tests"]["route_profile"]["status"] == "FAIL"
    assert not report["can_start_meeting_bridge"]
