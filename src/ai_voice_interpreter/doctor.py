from __future__ import annotations

import importlib
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .config import AppConfig

REQUIRED_PACKAGES = (
    "dashscope",
    "httpx",
    "numpy",
    "PySide6",
    "dotenv",
    "sounddevice",
    "soundfile",
)


class CheckLevel(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    level: CheckLevel
    name: str
    message: str


@dataclass(frozen=True, slots=True)
class DoctorReport:
    checks: tuple[DoctorCheck, ...]
    ready_for_real_mode: bool


def collect_checks(
    *,
    config_loader: Callable[[], AppConfig] | None = None,
    python_version: tuple[int, int, int] | None = None,
    platform_name: str | None = None,
    package_importer: Callable[[str], object] | None = None,
    microphone_probe: Callable[[], tuple[bool, str]] | None = None,
    afplay_path: Path = Path("/usr/bin/afplay"),
    temp_directory: Path | None = None,
) -> DoctorReport:
    """Inspect local readiness without making any model or network request."""
    checks: list[DoctorCheck] = []
    version = python_version or sys.version_info[:3]
    current_platform = platform_name or sys.platform
    importer = package_importer or importlib.import_module
    probe_microphone = microphone_probe or _probe_microphone
    load_config = config_loader or AppConfig.load

    supported_python = (3, 11) <= version[:2] < (3, 15)
    checks.append(
        DoctorCheck(
            CheckLevel.PASS if supported_python else CheckLevel.FAIL,
            "Python",
            f"{version[0]}.{version[1]}.{version[2]}"
            if supported_python
            else f"{version[0]}.{version[1]}.{version[2]}，需要 3.11–3.14",
        )
    )

    is_macos = current_platform == "darwin"
    checks.append(
        DoctorCheck(
            CheckLevel.PASS if is_macos else CheckLevel.FAIL,
            "macOS",
            "当前平台为 macOS" if is_macos else f"当前平台 {current_platform} 不受 MVP 支持",
        )
    )

    missing_packages = []
    for package in REQUIRED_PACKAGES:
        try:
            importer(package)
        except Exception:
            missing_packages.append(package)
    checks.append(
        DoctorCheck(
            CheckLevel.PASS if not missing_packages else CheckLevel.FAIL,
            "Python packages",
            "必要依赖均可导入"
            if not missing_packages
            else f"无法导入：{', '.join(missing_packages)}",
        )
    )

    config: AppConfig | None = None
    try:
        config = load_config()
        config.validate_basic()
        checks.append(DoctorCheck(CheckLevel.PASS, "Configuration", "项目配置加载成功"))
    except Exception as exc:
        checks.append(DoctorCheck(CheckLevel.FAIL, "Configuration", str(exc)))

    processing_configured = False
    real_mode = False
    if config is not None:
        real_mode = config.app_mode == "real"
        checks.append(
            DoctorCheck(
                CheckLevel.PASS if real_mode else CheckLevel.WARN,
                "APP_MODE",
                "real" if real_mode else f"当前为 {config.app_mode}，真实验收前应设为 real",
            )
        )
        if config.interpreter_mode == "remote":
            gateway_url = bool(config.ai_gateway_base_url)
            gateway_token = bool(config.ai_gateway_token)
            processing_configured = gateway_url and gateway_token
            checks.append(DoctorCheck(CheckLevel.PASS, "INTERPRETER_MODE", "remote"))
            checks.append(
                DoctorCheck(
                    CheckLevel.PASS if gateway_url else CheckLevel.FAIL,
                    "AI_GATEWAY_BASE_URL",
                    "已配置" if gateway_url else "未配置",
                )
            )
            checks.append(
                DoctorCheck(
                    CheckLevel.PASS if gateway_token else CheckLevel.WARN,
                    "AI_GATEWAY_TOKEN",
                    "已配置（值已隐藏）" if gateway_token else "未配置",
                )
            )
            checks.append(
                DoctorCheck(
                    CheckLevel.WARN if config.dashscope_api_key else CheckLevel.PASS,
                    "Local DASHSCOPE_API_KEY",
                    "不应在 Remote Mode 配置" if config.dashscope_api_key else "未配置（正确）",
                )
            )
        else:
            processing_configured = bool(config.dashscope_api_key)
            checks.append(DoctorCheck(CheckLevel.WARN, "INTERPRETER_MODE", "local 开发回退"))
            checks.append(
                DoctorCheck(
                    CheckLevel.PASS if processing_configured else CheckLevel.WARN,
                    "DASHSCOPE_API_KEY",
                    "已配置（值已隐藏）" if processing_configured else "未配置",
                )
            )
            checks.extend(_model_checks(config))

    microphone_ok, microphone_message = probe_microphone()
    checks.append(
        DoctorCheck(
            CheckLevel.PASS if microphone_ok else CheckLevel.FAIL,
            "Microphone",
            microphone_message,
        )
    )

    afplay_ok = afplay_path.is_file() and os.access(afplay_path, os.X_OK)
    checks.append(
        DoctorCheck(
            CheckLevel.PASS if afplay_ok else CheckLevel.FAIL,
            "afplay",
            "可执行" if afplay_ok else f"不可用：{afplay_path}",
        )
    )

    temp_ok, temp_message = _check_temp_directory(temp_directory)
    checks.append(
        DoctorCheck(
            CheckLevel.PASS if temp_ok else CheckLevel.FAIL,
            "Temporary directory",
            temp_message,
        )
    )

    has_failure = any(check.level == CheckLevel.FAIL for check in checks)
    ready = not has_failure and config is not None and real_mode and processing_configured
    return DoctorReport(tuple(checks), ready)


def _model_checks(config: AppConfig) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    models = (
        ("ASR", config.asr_provider, config.asr_model),
        ("Translation", config.translation_provider, config.translation_model),
        ("TTS", config.tts_provider, config.tts_model),
    )
    for name, provider, model in models:
        configured = bool(provider and model)
        checks.append(
            DoctorCheck(
                CheckLevel.PASS if configured else CheckLevel.FAIL,
                f"{name} model",
                f"provider={provider}, model={model}" if configured else "Provider 或模型未配置",
            )
        )

    if config.cloned_voice_id:
        voice_message = "克隆音色模式，ID 已隐藏"
    else:
        voice_message = f"系统音色 {config.effective_tts_voice}"
    checks.append(DoctorCheck(CheckLevel.PASS, "TTS voice", voice_message))
    checks.append(
        DoctorCheck(
            CheckLevel.PASS,
            "CLONED_VOICE_ID",
            "已配置（值已隐藏）" if config.cloned_voice_id else "未配置，使用系统音色",
        )
    )
    return checks


def _probe_microphone() -> tuple[bool, str]:
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        has_input = any(int(device.get("max_input_channels", 0)) > 0 for device in devices)
        if has_input:
            return True, "已发现麦克风输入设备"
        return False, "未发现麦克风输入设备"
    except Exception as exc:
        return False, f"设备查询失败：{exc}"


def _check_temp_directory(directory: Path | None) -> tuple[bool, str]:
    try:
        with tempfile.NamedTemporaryFile(
            dir=directory,
            prefix="aivi-doctor-",
            delete=True,
        ) as handle:
            handle.write(b"ok")
            handle.flush()
        return True, "可写"
    except Exception as exc:
        return False, f"不可写：{exc}"


def format_report(report: DoctorReport) -> str:
    lines = ["AI Voice Interpreter Doctor（不会调用任何收费模型）"]
    lines.extend(f"{check.level.value} {check.name}: {check.message}" for check in report.checks)
    if report.ready_for_real_mode:
        lines.append("PASS Real mode readiness: 可以启动真实模式")
    else:
        lines.append("WARN Real mode readiness: 尚未满足真实模式全部条件")
    return "\n".join(lines)


def main() -> int:
    print(format_report(collect_checks()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
