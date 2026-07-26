from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

from .config import AppConfig
from .exceptions import GatewayError
from .logging_config import configure_logging
from .stream_smoke import StreamingSmokeRunner


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="LiveTranslate through AI Voice Interpreter Gateway smoke"
    )
    parser.add_argument("--base-url")
    parser.add_argument("--token")
    parser.add_argument("--audio", type=Path)
    parser.add_argument("--microphone", action="store_true")
    parser.add_argument("--duration", type=float, default=20)
    parser.add_argument("--play", action="store_true")
    parser.add_argument("--keep-files", action="store_true")
    parser.add_argument(
        "--voice-mode", choices=("standard", "clone-once"), default="standard"
    )
    parser.add_argument("--json-report", type=Path)
    parser.add_argument("--max-turns", type=int, default=5)
    parser.add_argument("--no-source-transcription", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    if bool(args.audio) == bool(args.microphone):
        raise SystemExit("必须且只能选择 --audio 或 --microphone。")
    if args.max_turns < 1 or args.duration <= 0:
        raise SystemExit("--max-turns 和 --duration 必须大于 0。")
    overrides = dict(os.environ)
    if args.base_url:
        overrides["AI_GATEWAY_BASE_URL"] = args.base_url
    if args.token:
        overrides["AI_GATEWAY_TOKEN"] = args.token
    config = AppConfig.load(environ=overrides)
    config = replace(
        config,
        stream_voice_mode=(
            "clone_once" if args.voice_mode == "clone-once" else "standard"
        ),
        stream_capture_mode="headphones",
    )
    configure_logging(config.log_level)
    runner = StreamingSmokeRunner(
        config,
        play=args.play,
        keep_files=args.keep_files,
        source_transcription_enabled=not args.no_source_transcription,
    )
    exit_code = 0
    try:
        report = (
            runner.run_microphone(args.duration)
            if args.microphone
            else runner.run_file(args.audio)
        )
        if len(report.turn_ids) > args.max_turns:
            raise GatewayError("Smoke 产生的 Turn 超过 --max-turns 限制。")
        if report.pipeline_provider != "livetranslate":
            raise GatewayError(
                "Smoke 未使用 LiveTranslate，实际 Provider="
                f"{report.pipeline_provider or 'unknown'}。"
            )
    except Exception as exc:
        report = runner.report
        report.success = False
        report.error_code = type(exc).__name__
        report.error = str(exc)
        exit_code = 1
    finally:
        runner.close()
    payload: dict[str, Any] = {
        "success": exit_code == 0,
        "mode": "real_gateway_livetranslate",
        "voice_mode": config.stream_voice_mode,
        "source_transcription_enabled": not args.no_source_transcription,
        "playback_subjective_confirmation_required": bool(args.play),
        "run": asdict(report),
    }
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    print(rendered)
    if args.json_report:
        args.json_report.parent.mkdir(parents=True, exist_ok=True)
        args.json_report.write_text(rendered + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
