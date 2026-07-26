from __future__ import annotations

import argparse
import json
import math
import time
from pathlib import Path
from typing import Any

import numpy as np

from .audio_format import can_create_resampler
from .devices import AudioDeviceCatalog, AudioRouteProfile


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Non-model meeting audio route diagnostics")
    parser.add_argument(
        "--json-report",
        type=Path,
        default=Path("meeting-audio-doctor-output/report.json"),
    )
    parser.add_argument("--duration", type=float, default=3.0)
    return parser


def run_audio_checks(
    *,
    duration: float = 3.0,
    catalog: AudioDeviceCatalog | None = None,
    profile: AudioRouteProfile | None = None,
    sounddevice_module: Any | None = None,
) -> dict[str, Any]:
    catalog = catalog or AudioDeviceCatalog.discover(sounddevice_module)
    profile = profile if profile is not None else AudioRouteProfile.load()
    report: dict[str, Any] = {
        "paid_model_calls": 0,
        "tests": {},
        "can_start_meeting_bridge": False,
    }
    if profile is None or (route := profile.resolve(catalog)) is None:
        report["tests"]["route_profile"] = {
            "status": "FAIL",
            "message": "四设备路由未保存或设备无法重新解析。",
        }
        return report
    if sounddevice_module is None:
        import sounddevice as sounddevice_module

    report["route_map"] = route.safe_map()
    report["tests"]["physical_microphone"] = _microphone_test(
        sounddevice_module, route.local_microphone, duration
    )
    report["tests"]["blackhole_2ch_loopback"] = _loopback_test(
        sounddevice_module, route.meeting_virtual_microphone_output
    )
    report["tests"]["blackhole_16ch_loopback"] = _loopback_test(
        sounddevice_module, route.meeting_audio_capture_input
    )
    report["tests"]["headphones"] = _headphone_test(
        sounddevice_module, route.local_headphones_output
    )
    report["tests"]["cross_route_isolation"] = _cross_route_test(
        sounddevice_module,
        route.meeting_virtual_microphone_output,
        route.meeting_audio_capture_input,
    )
    report["tests"]["sample_rate"] = {
        "status": "PASS"
        if all(
            can_create_resampler(device.default_sample_rate, 16000, 1)
            and can_create_resampler(24000, device.default_sample_rate, 1)
            for device in (
                route.local_microphone,
                route.meeting_virtual_microphone_output,
                route.meeting_audio_capture_input,
                route.local_headphones_output,
            )
        )
        else "FAIL",
        "message": "四端点流式采样率转换检查完成。",
    }
    report["can_start_meeting_bridge"] = all(
        result["status"] in {"PASS", "NEEDS_USER_CONFIRMATION"}
        for result in report["tests"].values()
    )
    return report


def _microphone_test(sd: Any, device: Any, duration: float) -> dict[str, Any]:
    try:
        frames = max(1, int(device.default_sample_rate * duration))
        recording = sd.rec(
            frames,
            samplerate=device.default_sample_rate,
            channels=1,
            dtype="float32",
            device=device.index,
            blocking=True,
        )
        rms = float(np.sqrt(np.mean(np.square(recording))))
        peak = float(np.max(np.abs(recording)))
        return {
            "status": "PASS" if peak > 0.001 else "WARN",
            "rms": rms,
            "peak": peak,
            "audio_saved": False,
        }
    except Exception as exc:
        return {"status": "FAIL", "message": type(exc).__name__, "audio_saved": False}


def _signal(rate: float, duration: float = 0.6) -> np.ndarray:
    frames = int(rate * duration)
    phase = np.arange(frames, dtype=np.float32) / float(rate)
    mono = 0.03 * np.sin(2 * math.pi * 997 * phase)
    return np.column_stack((mono, mono)).astype(np.float32)


def _loopback_test(sd: Any, device: Any) -> dict[str, Any]:
    try:
        signal = _signal(device.default_sample_rate)
        captured = sd.playrec(
            signal,
            samplerate=device.default_sample_rate,
            channels=2,
            dtype="float32",
            device=(device.index, device.index),
            blocking=True,
        )
        rms = float(np.sqrt(np.mean(np.square(captured))))
        return {"status": "PASS" if rms > 0.001 else "FAIL", "rms": rms}
    except Exception as exc:
        return {"status": "FAIL", "message": type(exc).__name__}


def _headphone_test(sd: Any, device: Any) -> dict[str, Any]:
    try:
        sd.play(
            _signal(device.default_sample_rate, 0.35),
            samplerate=device.default_sample_rate,
            device=device.index,
            blocking=True,
        )
        return {
            "status": "NEEDS_USER_CONFIRMATION",
            "message": "低音量提示已只发送到所选耳机，请用户确认。",
        }
    except Exception as exc:
        return {"status": "FAIL", "message": type(exc).__name__}


def _cross_route_test(sd: Any, bh2: Any, bh16: Any) -> dict[str, Any]:
    try:
        signal = _signal(bh2.default_sample_rate)
        captured = sd.playrec(
            signal,
            samplerate=bh2.default_sample_rate,
            channels=2,
            dtype="float32",
            device=(bh16.index, bh2.index),
            blocking=True,
        )
        leakage_rms = float(np.sqrt(np.mean(np.square(captured))))
        return {
            "status": "PASS" if leakage_rms < 0.003 else "FAIL",
            "leakage_rms": leakage_rms,
        }
    except Exception as exc:
        return {"status": "FAIL", "message": type(exc).__name__}


def main() -> int:
    args = _parser().parse_args()
    started = time.monotonic()
    report = run_audio_checks(duration=args.duration)
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    args.json_report.parent.mkdir(parents=True, exist_ok=True)
    args.json_report.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
