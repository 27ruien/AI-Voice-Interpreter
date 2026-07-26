from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from ..config import AppConfig
from .audio_format import can_create_resampler, package_version
from .devices import AudioDeviceCatalog, AudioRouteProfile
from .route_guard import RouteGuard


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    level: str
    name: str
    message: str


def gateway_readyz(config: AppConfig) -> dict[str, object] | None:
    try:
        response = httpx.get(
            f"{config.ai_gateway_base_url.rstrip('/')}/readyz",
            timeout=min(10.0, config.network_timeout_seconds),
            follow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else None
    except Exception:
        return None


def collect_checks(
    config: AppConfig | None = None,
    catalog: AudioDeviceCatalog | None = None,
    profile: AudioRouteProfile | None = None,
    gateway_payload: dict[str, object] | None = None,
) -> tuple[list[DoctorCheck], bool]:
    config = config or AppConfig.load()
    catalog = catalog or AudioDeviceCatalog.discover()
    profile = profile if profile is not None else AudioRouteProfile.load()
    gateway_payload = gateway_payload or gateway_readyz(config)
    checks: list[DoctorCheck] = []
    bh2 = catalog.blackhole(2)
    bh16 = catalog.blackhole(16)
    microphones = catalog.microphones()
    headphones = catalog.headphones()
    checks.append(_presence("BlackHole 2ch", bh2, "未安装或安装后尚未重启"))
    checks.append(_presence("BlackHole 16ch", bh16, "未安装或安装后尚未重启"))
    checks.append(_presence("Physical Mic", microphones, "未发现物理输入设备"))
    checks.append(_presence("Headphones", headphones, "未发现物理耳机；不会使用扬声器代替"))
    checks.append(
        DoctorCheck(
            "PASS" if gateway_payload else "FAIL",
            "Gateway",
            "readyz 可访问" if gateway_payload else "readyz 不可访问",
        )
    )
    checks.append(
        DoctorCheck(
            "PASS" if config.ai_gateway_token else "FAIL",
            "Token",
            "Configured" if config.ai_gateway_token else "Not Configured",
        )
    )
    streaming = (
        gateway_payload.get("streaming", {}) if isinstance(gateway_payload, dict) else {}
    )
    if not isinstance(streaming, dict):
        streaming = {}
    capacity = int(streaming.get("streaming_max_connections_per_token", 0))
    supported = bool(streaming.get("bridge_sessions_supported"))
    checks.append(
        DoctorCheck(
            "PASS" if supported and capacity >= 2 else "FAIL",
            "双 WSS Session Capacity",
            f"supported={supported}, per_token={capacity}",
        )
    )
    rates = [device.default_sample_rate for device in catalog.devices if device.default_sample_rate]
    resampling_ok = bool(rates) and all(
        can_create_resampler(rate, 16000, 1)
        and can_create_resampler(24000, rate, 1)
        for rate in rates
    )
    checks.append(
        DoctorCheck(
            "PASS" if resampling_ok else "FAIL",
            "采样率转换能力",
            f"soxr={package_version()}",
        )
    )
    if profile is None:
        checks.append(DoctorCheck("WARN", "Audio Route Profile", "尚未保存四设备路由"))
    else:
        resolved = profile.resolve(catalog)
        if resolved is None:
            checks.append(DoctorCheck("FAIL", "Audio Route Profile", "保存设备无法重新解析"))
        else:
            guard = RouteGuard().validate(
                resolved,
                gateway_token_configured=bool(config.ai_gateway_token),
                gateway_ready=gateway_payload,
                meeting_setup_confirmed=profile.meeting_setup_confirmed,
            )
            checks.append(
                DoctorCheck(
                    "PASS" if guard.can_start else "FAIL",
                    "Audio Route Profile",
                    "无关键冲突"
                    if guard.can_start
                    else "；".join(item.code for item in guard.failures),
                )
            )
    ready = not any(check.level == "FAIL" for check in checks)
    return checks, ready


def _presence(name: str, devices: list[Any], failure: str) -> DoctorCheck:
    if not devices:
        return DoctorCheck("FAIL", name, failure)
    names = ", ".join(device.safe_name for device in devices)
    return DoctorCheck("PASS", name, names)


def format_report(checks: list[DoctorCheck], ready: bool) -> str:
    lines = ["AI Voice Interpreter Meeting Doctor（不会调用收费模型）"]
    lines.extend(f"{check.level} {check.name}: {check.message}" for check in checks)
    lines.append(
        ("PASS" if ready else "FAIL")
        + " Meeting Bridge readiness: "
        + ("可以开始" if ready else "不可开始，不会连接收费 Session")
    )
    return "\n".join(lines)


def main() -> int:
    checks, ready = collect_checks()
    print(format_report(checks, ready))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
