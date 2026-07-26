from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

BLACKHOLE_PATTERN = re.compile(r"black\s*hole", re.IGNORECASE)
CHANNEL_PATTERN = re.compile(r"\b(2|16)\s*(?:ch|channels?)\b", re.IGNORECASE)
HEADPHONE_PATTERN = re.compile(
    r"headphones?|headset|airpods?|earbuds?|耳机|耳麦", re.IGNORECASE
)
VIRTUAL_PATTERN = re.compile(
    r"black\s*hole|virtual|lark|teams audio|zoomaudio|aggregate|multi-output",
    re.IGNORECASE,
)


def normalize_device_name(name: str) -> str:
    return " ".join(name.casefold().split())


@dataclass(frozen=True, slots=True)
class AudioDeviceInfo:
    index: int
    stable_key: str
    name: str
    host_api: str
    max_input_channels: int
    max_output_channels: int
    default_sample_rate: float
    is_virtual: bool
    is_blackhole: bool
    blackhole_channels: int | None
    is_headphones_candidate: bool
    is_microphone_candidate: bool
    coreaudio_uid: str | None = None

    @classmethod
    def from_sounddevice(
        cls,
        index: int,
        raw: dict[str, Any],
        host_api: str,
        *,
        coreaudio_uid: str | None = None,
    ) -> AudioDeviceInfo:
        name = str(raw.get("name", f"Device {index}"))
        inputs = int(raw.get("max_input_channels", 0))
        outputs = int(raw.get("max_output_channels", 0))
        rate = float(raw.get("default_samplerate", 0.0))
        is_blackhole = bool(BLACKHOLE_PATTERN.search(name))
        match = CHANNEL_PATTERN.search(name)
        blackhole_channels = int(match.group(1)) if is_blackhole and match else None
        if is_blackhole and blackhole_channels is None:
            capacity = max(inputs, outputs)
            blackhole_channels = 16 if capacity >= 16 else 2 if capacity >= 2 else None
        is_virtual = bool(VIRTUAL_PATTERN.search(name))
        stable = coreaudio_uid or "|".join(
            (
                normalize_device_name(name),
                normalize_device_name(host_api),
                f"in:{inputs}",
                f"out:{outputs}",
                f"rate:{round(rate)}",
            )
        )
        return cls(
            index=index,
            stable_key=stable,
            name=name,
            host_api=host_api,
            max_input_channels=inputs,
            max_output_channels=outputs,
            default_sample_rate=rate,
            is_virtual=is_virtual,
            is_blackhole=is_blackhole,
            blackhole_channels=blackhole_channels,
            is_headphones_candidate=bool(outputs and HEADPHONE_PATTERN.search(name)),
            is_microphone_candidate=bool(inputs and not is_virtual),
            coreaudio_uid=coreaudio_uid,
        )

    @property
    def safe_name(self) -> str:
        return f"{self.name} ({self.host_api})"


class AudioDeviceCatalog:
    def __init__(self, devices: list[AudioDeviceInfo]) -> None:
        self.devices = list(devices)

    @classmethod
    def discover(cls, sounddevice_module: Any | None = None) -> AudioDeviceCatalog:
        if sounddevice_module is None:
            import sounddevice as sounddevice_module

        host_apis = sounddevice_module.query_hostapis()
        devices: list[AudioDeviceInfo] = []
        for index, raw in enumerate(sounddevice_module.query_devices()):
            host_index = int(raw.get("hostapi", -1))
            try:
                host_name = str(host_apis[host_index]["name"])
            except (IndexError, KeyError, TypeError):
                host_name = f"Host API {host_index}"
            devices.append(AudioDeviceInfo.from_sounddevice(index, dict(raw), host_name))
        return cls(devices)

    def blackhole(self, channels: int) -> list[AudioDeviceInfo]:
        return [
            device
            for device in self.devices
            if device.is_blackhole and device.blackhole_channels == channels
        ]

    def microphones(self) -> list[AudioDeviceInfo]:
        return [device for device in self.devices if device.is_microphone_candidate]

    def headphones(self) -> list[AudioDeviceInfo]:
        return [device for device in self.devices if device.is_headphones_candidate]

    def resolve(self, stable_key: str) -> AudioDeviceInfo | None:
        matches = [device for device in self.devices if device.stable_key == stable_key]
        return matches[0] if len(matches) == 1 else None


@dataclass(frozen=True, slots=True)
class AudioRouteProfile:
    local_microphone: str
    meeting_virtual_microphone_output: str
    meeting_audio_capture_input: str
    local_headphones_output: str
    local_to_remote_voice: str = "Tina"
    remote_to_local_voice: str = "Ethan"
    route_version: int = 1
    meeting_setup_confirmed: bool = False

    @classmethod
    def load(cls, path: Path | None = None) -> AudioRouteProfile | None:
        selected = path or default_audio_routes_path()
        if not selected.exists():
            return None
        try:
            data = json.loads(selected.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return None
            allowed = {field.name for field in cls.__dataclass_fields__.values()}
            return cls(**{key: value for key, value in data.items() if key in allowed})
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def save(self, path: Path | None = None) -> Path:
        selected = path or default_audio_routes_path()
        selected.parent.mkdir(parents=True, exist_ok=True)
        temporary = selected.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(selected)
        return selected

    def resolve(self, catalog: AudioDeviceCatalog) -> ResolvedAudioRoute | None:
        devices = (
            catalog.resolve(self.local_microphone),
            catalog.resolve(self.meeting_virtual_microphone_output),
            catalog.resolve(self.meeting_audio_capture_input),
            catalog.resolve(self.local_headphones_output),
        )
        if any(device is None for device in devices):
            return None
        microphone, virtual_output, meeting_input, headphones = devices
        assert microphone and virtual_output and meeting_input and headphones
        return ResolvedAudioRoute(microphone, virtual_output, meeting_input, headphones)


@dataclass(frozen=True, slots=True)
class ResolvedAudioRoute:
    local_microphone: AudioDeviceInfo
    meeting_virtual_microphone_output: AudioDeviceInfo
    meeting_audio_capture_input: AudioDeviceInfo
    local_headphones_output: AudioDeviceInfo

    def safe_map(self) -> dict[str, str]:
        return {
            "local_microphone": self.local_microphone.safe_name,
            "meeting_virtual_microphone_output": (
                self.meeting_virtual_microphone_output.safe_name
            ),
            "meeting_audio_capture_input": self.meeting_audio_capture_input.safe_name,
            "local_headphones_output": self.local_headphones_output.safe_name,
        }


def default_audio_routes_path() -> Path:
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "AI Voice Interpreter"
        / "audio_routes.json"
    )
