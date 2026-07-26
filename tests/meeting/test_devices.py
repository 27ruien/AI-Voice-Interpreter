from __future__ import annotations

import json
from pathlib import Path

from ai_voice_interpreter.meeting.devices import (
    AudioDeviceCatalog,
    AudioDeviceInfo,
    AudioRouteProfile,
    normalize_device_name,
)


class FakeSoundDevice:
    @staticmethod
    def query_hostapis():  # type: ignore[no-untyped-def]
        return [{"name": "Core Audio"}]

    @staticmethod
    def query_devices():  # type: ignore[no-untyped-def]
        return [
            {
                "name": "Built-in Microphone",
                "hostapi": 0,
                "max_input_channels": 1,
                "max_output_channels": 0,
                "default_samplerate": 44100,
            },
            {
                "name": "BlackHole 2ch (Virtual)",
                "hostapi": 0,
                "max_input_channels": 2,
                "max_output_channels": 2,
                "default_samplerate": 48000,
            },
            {
                "name": "Black Hole 16 channels",
                "hostapi": 0,
                "max_input_channels": 16,
                "max_output_channels": 16,
                "default_samplerate": 48000,
            },
            {
                "name": "USB Headphones",
                "hostapi": 0,
                "max_input_channels": 0,
                "max_output_channels": 2,
                "default_samplerate": 48000,
            },
        ]


def test_discovery_classifies_blackhole_variants_and_candidates() -> None:
    catalog = AudioDeviceCatalog.discover(FakeSoundDevice)
    assert catalog.blackhole(2)[0].name == "BlackHole 2ch (Virtual)"
    assert catalog.blackhole(16)[0].name == "Black Hole 16 channels"
    assert catalog.microphones()[0].name == "Built-in Microphone"
    assert catalog.headphones()[0].name == "USB Headphones"


def test_stable_key_survives_portaudio_index_change() -> None:
    raw = {
        "name": "BlackHole 2ch",
        "max_input_channels": 2,
        "max_output_channels": 2,
        "default_samplerate": 48000,
    }
    first = AudioDeviceInfo.from_sounddevice(3, raw, "Core Audio")
    second = AudioDeviceInfo.from_sounddevice(11, raw, "Core Audio")
    assert first.stable_key == second.stable_key


def test_duplicate_stable_key_is_not_resolved_ambiguously() -> None:
    catalog = AudioDeviceCatalog.discover(FakeSoundDevice)
    original = catalog.devices[0]
    catalog.devices.append(original)
    assert catalog.resolve(original.stable_key) is None


def test_missing_device_returns_none() -> None:
    assert AudioDeviceCatalog([]).resolve("missing") is None


def test_name_normalization_is_case_and_whitespace_insensitive() -> None:
    assert normalize_device_name(" BlackHole   2CH ") == "blackhole 2ch"


def test_profile_round_trip_and_re_resolve(tmp_path: Path) -> None:
    catalog = AudioDeviceCatalog.discover(FakeSoundDevice)
    mic, bh2, bh16, headphones = catalog.devices
    profile = AudioRouteProfile(
        mic.stable_key,
        bh2.stable_key,
        bh16.stable_key,
        headphones.stable_key,
        meeting_setup_confirmed=True,
    )
    path = tmp_path / "audio_routes.json"
    profile.save(path)
    loaded = AudioRouteProfile.load(path)
    assert loaded == profile
    assert loaded and loaded.resolve(catalog) is not None
    assert path.stat().st_mode & 0o777 == 0o600
    payload = json.loads(path.read_text())
    assert "token" not in json.dumps(payload).lower()
    assert "api_key" not in json.dumps(payload).lower()


def test_invalid_profile_does_not_select_arbitrary_device(tmp_path: Path) -> None:
    path = tmp_path / "audio_routes.json"
    path.write_text('{"local_microphone":"missing"}', encoding="utf-8")
    assert AudioRouteProfile.load(path) is None


def test_channel_capabilities_are_retained() -> None:
    catalog = AudioDeviceCatalog.discover(FakeSoundDevice)
    bh16 = catalog.blackhole(16)[0]
    assert bh16.max_input_channels == 16
    assert bh16.max_output_channels == 16
    assert bh16.host_api == "Core Audio"
