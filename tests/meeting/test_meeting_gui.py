from __future__ import annotations

from PySide6.QtWidgets import QApplication

from ai_voice_interpreter.audio import MacAudioPlayer, MicrophoneRecorder
from ai_voice_interpreter.config import AppConfig
from ai_voice_interpreter.main import build_pipeline
from ai_voice_interpreter.meeting.devices import AudioDeviceCatalog
from ai_voice_interpreter.meeting.smoke import mock_route
from ai_voice_interpreter.ui import MainWindow


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


def window(monkeypatch) -> MainWindow:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        AudioDeviceCatalog,
        "discover",
        classmethod(lambda cls, sounddevice_module=None: catalog()),
    )
    app = QApplication.instance() or QApplication([])
    del app
    config = AppConfig(app_mode="mock", ai_gateway_token="mock-token")
    return MainWindow(
        config,
        MicrophoneRecorder(),
        MacAudioPlayer(),
        build_pipeline(config),
    )


def test_meeting_mode_has_explicit_four_device_selection(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    ui = window(monkeypatch)
    ui.mode_combo.setCurrentIndex(ui.mode_combo.findData("meeting_bridge"))
    assert not ui.meeting_frame.isHidden()
    assert not ui.capture_combo.isEnabled()
    assert not ui.voice_combo.isEnabled()
    assert ui.start_button.text() == "Start Meeting Bridge"
    assert set(ui.meeting_device_combos) == {
        "local_microphone",
        "meeting_virtual_microphone_output",
        "meeting_audio_capture_input",
        "local_headphones_output",
    }
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
