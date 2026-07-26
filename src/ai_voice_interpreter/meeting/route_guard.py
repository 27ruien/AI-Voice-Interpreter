from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .audio_format import can_create_resampler
from .devices import AudioDeviceInfo, ResolvedAudioRoute


@dataclass(frozen=True, slots=True)
class RouteCheck:
    code: str
    status: str
    message: str
    critical: bool = True


@dataclass(frozen=True, slots=True)
class RouteGuardResult:
    checks: tuple[RouteCheck, ...]

    @property
    def can_start(self) -> bool:
        return not any(check.critical and check.status == "FAIL" for check in self.checks)

    @property
    def failures(self) -> tuple[RouteCheck, ...]:
        return tuple(check for check in self.checks if check.status == "FAIL")


class AudioLoopGuard:
    @staticmethod
    def validate(route: ResolvedAudioRoute) -> list[RouteCheck]:
        mic = route.local_microphone
        virtual = route.meeting_virtual_microphone_output
        capture = route.meeting_audio_capture_input
        headphones = route.local_headphones_output
        checks = [
            _different(
                "LOOP_INPUTS_DISTINCT",
                mic,
                capture,
                "本地麦克风与会议捕获输入必须不同。",
            ),
            _different(
                "LOOP_OUTPUTS_DISTINCT",
                virtual,
                headphones,
                "会议虚拟麦克风与本地耳机输出必须不同。",
            ),
            _different(
                "LOOP_LOCAL_DIRECTION",
                mic,
                virtual,
                "A→B 输入和输出不能是同一设备。",
            ),
            _different(
                "LOOP_REMOTE_DIRECTION",
                capture,
                headphones,
                "B→A 输入和输出不能是同一设备。",
            ),
            _different(
                "BLACKHOLE_CROSS_ROUTE",
                virtual,
                capture,
                "BlackHole 2ch 与 BlackHole 16ch 必须是不同设备。",
            ),
        ]
        return checks


class RouteGuard:
    def __init__(
        self,
        settings_check: Callable[[AudioDeviceInfo, str], bool] | None = None,
    ) -> None:
        self.settings_check = settings_check or _default_settings_check

    def validate(
        self,
        route: ResolvedAudioRoute,
        *,
        gateway_token_configured: bool,
        gateway_ready: dict[str, object] | None,
        meeting_setup_confirmed: bool,
    ) -> RouteGuardResult:
        mic = route.local_microphone
        virtual = route.meeting_virtual_microphone_output
        capture = route.meeting_audio_capture_input
        headphones = route.local_headphones_output
        streaming = (
            gateway_ready.get("streaming", {}) if isinstance(gateway_ready, dict) else {}
        )
        if not isinstance(streaming, dict):
            streaming = {}
        checks = [
            _capacity("MIC_INPUT", mic.max_input_channels > 0, "物理麦克风必须有输入声道。"),
            _capacity(
                "VIRTUAL_MIC_OUTPUT",
                virtual.max_output_channels >= 2,
                "BlackHole 2ch 必须有至少两个输出声道。",
            ),
            _capacity(
                "MEETING_CAPTURE_INPUT",
                capture.max_input_channels >= 2,
                "BlackHole 16ch 必须有至少两个输入声道。",
            ),
            _capacity(
                "HEADPHONES_OUTPUT",
                headphones.max_output_channels > 0,
                "本地耳机必须有输出声道。",
            ),
            _capacity(
                "VIRTUAL_MIC_IS_BLACKHOLE_2CH",
                virtual.is_blackhole and virtual.blackhole_channels == 2,
                "发送给会议的设备必须是 BlackHole 2ch。",
            ),
            _capacity(
                "MEETING_CAPTURE_IS_BLACKHOLE_16CH",
                capture.is_blackhole and capture.blackhole_channels == 16,
                "会议捕获设备必须是 BlackHole 16ch。",
            ),
            _capacity(
                "MIC_IS_PHYSICAL",
                not mic.is_blackhole and mic.is_microphone_candidate,
                "我的麦克风不能选择 BlackHole 或虚拟设备。",
            ),
            _capacity(
                "HEADPHONES_ARE_PHYSICAL",
                not headphones.is_blackhole and headphones.is_headphones_candidate,
                "会议桥接模式必须选择物理耳机，不能使用扬声器或 BlackHole。",
            ),
            _capacity(
                "INPUT_RATE_CONVERSION",
                can_create_resampler(mic.default_sample_rate, 16000, 1)
                and can_create_resampler(capture.default_sample_rate, 16000, 1),
                "输入设备采样率必须可转换为 16 kHz。",
            ),
            _capacity(
                "OUTPUT_RATE_CONVERSION",
                can_create_resampler(24000, virtual.default_sample_rate, 1)
                and can_create_resampler(24000, headphones.default_sample_rate, 1),
                "24 kHz 翻译音频必须可转换到两个输出设备。",
            ),
            _capacity(
                "DEVICE_OPEN_CAPABILITY",
                all(
                    (
                        self.settings_check(mic, "input"),
                        self.settings_check(virtual, "output"),
                        self.settings_check(capture, "input"),
                        self.settings_check(headphones, "output"),
                    )
                ),
                "至少一个设备无法按声明的采样率和声道打开。",
            ),
            _capacity(
                "GATEWAY_TOKEN",
                gateway_token_configured,
                "AI_GATEWAY_TOKEN 未配置。",
            ),
            _capacity(
                "GATEWAY_BRIDGE_SUPPORT",
                bool(streaming.get("bridge_sessions_supported")),
                "Gateway 尚未启用 Meeting Bridge。",
            ),
            _capacity(
                "GATEWAY_TWO_SESSIONS",
                int(streaming.get("streaming_max_connections_per_token", 0)) >= 2,
                "Gateway 必须允许同 Token 两条流式 Session。",
            ),
            _capacity(
                "MEETING_SETUP_CONFIRMED",
                meeting_setup_confirmed,
                "请先确认会议软件 Microphone=BlackHole 2ch、Speaker=BlackHole 16ch。",
            ),
        ]
        checks.extend(AudioLoopGuard.validate(route))
        return RouteGuardResult(tuple(checks))


def _capacity(code: str, valid: bool, message: str) -> RouteCheck:
    return RouteCheck(code, "PASS" if valid else "FAIL", message)


def _different(
    code: str,
    first: AudioDeviceInfo,
    second: AudioDeviceInfo,
    message: str,
) -> RouteCheck:
    return _capacity(code, first.stable_key != second.stable_key, message)


def _default_settings_check(device: AudioDeviceInfo, direction: str) -> bool:
    try:
        import sounddevice as sd

        if direction == "input":
            channels = min(2, device.max_input_channels)
            sd.check_input_settings(
                device=device.index,
                channels=channels,
                dtype="float32",
                samplerate=device.default_sample_rate,
            )
        else:
            channels = min(2, device.max_output_channels)
            sd.check_output_settings(
                device=device.index,
                channels=channels,
                dtype="float32",
                samplerate=device.default_sample_rate,
            )
        return channels > 0
    except Exception:
        return False
