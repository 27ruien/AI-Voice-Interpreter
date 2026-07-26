from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import QApplication

from ai_voice_interpreter.audio import MacAudioPlayer, MicrophoneRecorder
from ai_voice_interpreter.config import AppConfig
from ai_voice_interpreter.main import build_pipeline
from ai_voice_interpreter.meeting.devices import AudioDeviceCatalog, AudioRouteProfile
from ai_voice_interpreter.meeting.route_guard import (
    RouteCheck,
    RouteGuard,
    RouteGuardResult,
)
from ai_voice_interpreter.meeting.smoke import mock_route
from ai_voice_interpreter.ui import MainWindow
from ai_voice_interpreter.ui import main_window as main_window_module
from ai_voice_interpreter.ui import workers as workers_module
from ai_voice_interpreter.ui.workers import MeetingAudioCheckWorker


def catalog() -> AudioDeviceCatalog:
    route = mock_route()
    return AudioDeviceCatalog(
        [
            route.local_microphone,
            route.meeting_virtual_microphone_output,
            route.meeting_audio_capture_input,
            route.local_headphones_output,
        ]
    )


def window(
    monkeypatch, selected_catalog: AudioDeviceCatalog | None = None
) -> MainWindow:  # type: ignore[no-untyped-def]
    selected_catalog = selected_catalog or catalog()
    monkeypatch.setattr(
        AudioDeviceCatalog,
        "discover",
        classmethod(lambda cls, sounddevice_module=None: selected_catalog),
    )
    monkeypatch.setattr(AudioRouteProfile, "load", classmethod(lambda cls, path=None: None))
    app = QApplication.instance() or QApplication([])
    del app
    config = AppConfig(app_mode="mock", ai_gateway_token="mock-token")
    return MainWindow(
        config,
        MicrophoneRecorder(),
        MacAudioPlayer(),
        build_pipeline(config),
    )


def select_all_devices(ui: MainWindow) -> None:
    for combo in ui.meeting_device_combos.values():
        assert combo.count() == 2
        combo.setCurrentIndex(1)


def test_meeting_mode_has_explicit_four_device_selection(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ui = window(monkeypatch)
    ui.mode_combo.setCurrentIndex(ui.mode_combo.findData("meeting_bridge"))
    assert not ui.meeting_frame.isHidden()
    assert not ui.capture_combo.isEnabled()
    assert not ui.voice_combo.isEnabled()
    assert ui.start_button.text() == "Start Meeting Bridge"
    assert ui.meeting_frame.title() == "Meeting Bridge Setup"
    assert ui.meeting_refresh_button.text() == "Refresh Devices"
    assert ui.meeting_audio_check_button.text() == "Run Audio Check"
    assert ui.meeting_save_button.text() == "Save Route Profile"
    assert set(ui.meeting_device_combos) == {
        "local_microphone",
        "meeting_virtual_microphone_output",
        "meeting_audio_capture_input",
        "local_headphones_output",
    }
    assert all(not combo.currentData() for combo in ui.meeting_device_combos.values())
    assert not ui.meeting_save_button.isEnabled()
    assert not ui.meeting_audio_check_button.isEnabled()
    ui.close()


def test_saved_route_selection_re_resolves_without_arbitrary_fallback(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ui = window(monkeypatch)
    route = mock_route()
    selected = {
        "local_microphone": route.local_microphone.stable_key,
        "meeting_virtual_microphone_output": (
            route.meeting_virtual_microphone_output.stable_key
        ),
        "meeting_audio_capture_input": route.meeting_audio_capture_input.stable_key,
        "local_headphones_output": route.local_headphones_output.stable_key,
    }
    for key, stable_key in selected.items():
        combo = ui.meeting_device_combos[key]
        combo.setCurrentIndex(combo.findData(stable_key))
    ui.meeting_setup_confirmed.setChecked(True)
    profile = ui._selected_meeting_profile()  # noqa: SLF001
    assert profile.resolve(catalog()) == route
    assert profile.meeting_setup_confirmed
    ui.close()


def test_refresh_rescans_devices_without_automatic_selection(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ui = window(monkeypatch)
    calls = 0

    def discover(cls, sounddevice_module=None):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        return catalog()

    monkeypatch.setattr(AudioDeviceCatalog, "discover", classmethod(discover))
    ui.meeting_refresh_button.click()
    assert calls == 1
    assert all(not combo.currentData() for combo in ui.meeting_device_combos.values())
    ui.close()


def test_missing_headphones_disables_save_and_audio_check(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    without_headphones = AudioDeviceCatalog(catalog().devices[:-1])
    ui = window(monkeypatch, without_headphones)
    assert ui.meeting_device_combos["local_headphones_output"].count() == 1
    assert not ui.meeting_save_button.isEnabled()
    assert not ui.meeting_audio_check_button.isEnabled()
    ui.close()


def test_save_runs_route_guard_and_persists_expected_profile(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    ui = window(monkeypatch)
    select_all_devices(ui)
    ui.meeting_setup_confirmed.setChecked(True)
    monkeypatch.setattr(main_window_module, "gateway_readyz", lambda _config: {
        "streaming": {
            "bridge_sessions_supported": True,
            "streaming_max_connections_per_token": 2,
        }
    })
    monkeypatch.setattr(
        main_window_module,
        "RouteGuard",
        lambda: RouteGuard(settings_check=lambda _device, _direction: True),
    )
    destination = tmp_path / "audio_routes.json"
    original_save = AudioRouteProfile.save
    monkeypatch.setattr(
        AudioRouteProfile,
        "save",
        lambda self, path=None: original_save(self, destination),
    )

    ui.meeting_save_button.click()

    assert destination.is_file()
    assert destination.stat().st_mode & 0o777 == 0o600
    payload = json.loads(destination.read_text(encoding="utf-8"))
    assert payload == {
        "local_microphone": "mock-1",
        "meeting_virtual_microphone_output": "mock-2",
        "meeting_audio_capture_input": "mock-3",
        "local_headphones_output": "mock-4",
        "local_to_remote_voice": "Tina",
        "remote_to_local_voice": "Ethan",
        "route_version": 1,
        "meeting_setup_confirmed": True,
    }
    assert ui.meeting_route_status.text() == "Meeting audio route saved"
    assert "Meeting audio route saved" in ui.error_text.toPlainText()
    ui.close()


def test_route_guard_failure_does_not_save_and_shows_reason(
    monkeypatch, tmp_path: Path
) -> None:  # type: ignore[no-untyped-def]
    ui = window(monkeypatch)
    select_all_devices(ui)
    ui.meeting_setup_confirmed.setChecked(True)
    monkeypatch.setattr(main_window_module, "gateway_readyz", lambda _config: {})

    class FailingGuard:
        def validate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return RouteGuardResult(
                (
                    RouteCheck(
                        "HEADPHONES_ARE_PHYSICAL",
                        "FAIL",
                        "会议桥接模式必须使用物理耳机。",
                    ),
                )
            )

    monkeypatch.setattr(main_window_module, "RouteGuard", FailingGuard)
    destination = tmp_path / "must-not-exist.json"
    monkeypatch.setattr(
        AudioRouteProfile,
        "save",
        lambda self, path=None: destination,
    )

    ui.meeting_save_button.click()

    assert not destination.exists()
    assert ui.meeting_route_status.text() == "Audio Route Profile not saved"
    assert "HEADPHONES_ARE_PHYSICAL" in ui.error_text.toPlainText()
    ui.close()


def test_run_audio_check_starts_existing_check_worker(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ui = window(monkeypatch)
    select_all_devices(ui)
    started: list[object] = []
    monkeypatch.setattr(ui, "_start_worker", started.append)

    ui.meeting_audio_check_button.click()

    assert len(started) == 1
    assert isinstance(started[0], MeetingAudioCheckWorker)
    assert "Running local meeting audio checks" in ui.error_text.toPlainText()
    ui.close()


def test_audio_check_worker_uses_existing_local_check_logic(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    expected = {
        "paid_model_calls": 0,
        "tests": {"sample_rate": {"status": "PASS"}},
        "can_start_meeting_bridge": True,
    }
    monkeypatch.setattr(workers_module, "run_audio_checks", lambda **_kwargs: expected)
    route = mock_route()
    profile = AudioRouteProfile(
        route.local_microphone.stable_key,
        route.meeting_virtual_microphone_output.stable_key,
        route.meeting_audio_capture_input.stable_key,
        route.local_headphones_output.stable_key,
        meeting_setup_confirmed=True,
    )
    worker = MeetingAudioCheckWorker(catalog(), profile)
    reports: list[object] = []
    finished: list[bool] = []
    worker.report_ready.connect(reports.append)
    worker.finished.connect(lambda: finished.append(True))

    worker.run()

    assert reports == [expected]
    assert finished == [True]


def test_start_requires_previously_saved_validated_profile(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ui = window(monkeypatch)
    select_all_devices(ui)
    ui.meeting_setup_confirmed.setChecked(True)
    save_calls = 0

    def unexpected_save(self, path=None):  # type: ignore[no-untyped-def]
        nonlocal save_calls
        save_calls += 1
        return Path("unexpected")

    monkeypatch.setattr(AudioRouteProfile, "save", unexpected_save)
    ui._start_meeting_bridge()  # noqa: SLF001
    assert save_calls == 0
    assert "Save Route Profile" in ui.error_text.toPlainText()
    ui.close()


def test_direction_cards_show_degraded_and_clear_listening_after_stop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ui = window(monkeypatch)
    event = {
        "type": "session.started",
        "pipeline_provider": "livetranslate",
        "bridge_id": "bridge-id",
    }
    ui._handle_meeting_event("local_to_remote", event)  # noqa: SLF001
    assert "Listening" in ui.meeting_direction_labels["local_to_remote"]["state"].text()
    ui._meeting_state_changed("DEGRADED")  # noqa: SLF001
    assert "DEGRADED" in ui.meeting_global_label.text()
    ui._meeting_state_changed("STOPPED")  # noqa: SLF001
    assert all(
        labels["state"].text() == "Disconnected"
        for labels in ui.meeting_direction_labels.values()
    )
    ui.close()
