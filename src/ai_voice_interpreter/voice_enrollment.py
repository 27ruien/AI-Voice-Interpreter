from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .config import AppConfig, default_user_config_path
from .exceptions import ConfigurationError, VoiceEnrollmentError
from .providers.common import configure_dashscope, friendly_service_message

PREFIX_PATTERN = re.compile(r"^[A-Za-z0-9]{1,10}$")


@dataclass(frozen=True, slots=True)
class EnrollmentResult:
    voice_id: str
    request_id: str | None


def validate_enrollment_arguments(audio_url: str, prefix: str, language: str) -> None:
    parsed = urlparse(audio_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise VoiceEnrollmentError("--audio-url 必须是公网可访问的 HTTP/HTTPS URL。")
    if not PREFIX_PATTERN.fullmatch(prefix):
        raise VoiceEnrollmentError("--prefix 仅允许 1–10 个英文字母或数字。")
    if language not in {"zh", "en", "fr", "de", "ja", "ko", "ru", "pt", "th", "id", "vi"}:
        raise VoiceEnrollmentError(f"不支持的录音语言提示：{language}")


def enroll_voice(
    config: AppConfig,
    audio_url: str,
    prefix: str,
    language: str = "zh",
    service_factory: Callable[[], object] | None = None,
) -> EnrollmentResult:
    validate_enrollment_arguments(audio_url, prefix, language)
    if not config.dashscope_api_key:
        raise ConfigurationError("缺少 DASHSCOPE_API_KEY，无法创建克隆音色。")
    try:
        configure_dashscope(config)
        if service_factory is None:
            from dashscope.audio.tts_v2 import VoiceEnrollmentService

            service_factory = VoiceEnrollmentService
        service = service_factory()
        voice_id = service.create_voice(
            target_model=config.tts_model,
            prefix=prefix,
            url=audio_url,
            language_hints=[language],
        )
        if not voice_id:
            raise VoiceEnrollmentError("声音复刻服务未返回 voice_id。")
        request_id = service.get_last_request_id()
        if not str(voice_id).startswith(f"{config.tts_model}-"):
            raise VoiceEnrollmentError("返回的音色与 TTS_MODEL 不匹配，请勿用于合成。")
        return EnrollmentResult(str(voice_id), str(request_id) if request_id else None)
    except (ConfigurationError, VoiceEnrollmentError):
        raise
    except Exception as exc:
        raise VoiceEnrollmentError(friendly_service_message(exc)) from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用 DashScope 为 CosyVoice 创建克隆音色")
    parser.add_argument("--audio-url", required=True, help="公网可访问的 10–20 秒清晰人声音频 URL")
    parser.add_argument("--prefix", required=True, help="1–10 个英文字母或数字")
    parser.add_argument("--language", default="zh", help="录音语言提示，中文使用 zh")
    parser.add_argument(
        "--write-config",
        action="store_true",
        help="将 CLONED_VOICE_ID 和 TTS_MODEL 写入本机用户配置（不会写入仓库）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print("请确认：只能复刻你本人的声音，或已获得声音所有者明确授权的声音。")
    try:
        config = AppConfig.load()
        result = enroll_voice(config, args.audio_url, args.prefix, args.language)
        print(f"Voice ID: {result.voice_id}")
        if result.request_id:
            print(f"Request ID: {result.request_id}")
        if args.write_config:
            path = write_user_config(result.voice_id, config.tts_model)
            print(f"已写入本机配置：{path}")
        print(f"请设置 CLONED_VOICE_ID={result.voice_id}")
        return 0
    except (ConfigurationError, VoiceEnrollmentError) as exc:
        print(f"声音复刻失败：{exc}", file=sys.stderr)
        return 1


def write_user_config(voice_id: str, tts_model: str, path: Path | None = None) -> Path:
    target = path or default_user_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    updates = {"CLONED_VOICE_ID": voice_id, "TTS_MODEL": tts_model}
    output: list[str] = []
    seen: set[str] = set()
    for line in existing:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in updates:
            output.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={value}")
    target.write_text("\n".join(output).rstrip() + "\n", encoding="utf-8")
    target.chmod(0o600)
    return target


if __name__ == "__main__":
    raise SystemExit(main())

