from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LIVE_REPORT = Path("livetranslate-smoke-output/report.json")
MODULAR_REPORT = Path("stream-smoke-output/report.json")
OUTPUT_DIR = Path("pipeline-benchmark-output")


def _load_live(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    run = payload.get("run")
    if not payload.get("success") or not isinstance(run, dict):
        return None
    return run


def _load_modular(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    runs = payload.get("runs")
    if not isinstance(runs, list):
        return None
    successful = [run for run in runs if isinstance(run, dict) and run.get("success")]
    return successful[-1] if successful else None


def build_report(
    live: dict[str, Any] | None, modular: dict[str, Any] | None
) -> dict[str, Any]:
    return {
        "comparison_kind": "controlled_real_smoke_only",
        "percentiles": "not_calculated_single_or_missing_sample",
        "livetranslate": _summary(live),
        "modular": _summary(modular),
        "notes": [
            "Mock latency is intentionally excluded.",
            "Missing real samples are reported as unavailable, not estimated.",
            "Subjective voice quality requires user confirmation.",
        ],
    }


def _summary(run: dict[str, Any] | None) -> dict[str, Any]:
    if run is None:
        return {"sample_count": 0, "status": "unavailable"}
    return {
        "sample_count": 1,
        "status": "available",
        "provider": run.get("pipeline_provider"),
        "speech_end_to_first_translation_ms": run.get("first_translation_ms"),
        "speech_end_to_first_audio_ms": run.get("first_audio_ms"),
        "client_first_playback_ms": run.get("client_first_playback_ms"),
        "end_to_end_ttfa_ms": run.get("end_to_end_ttfa_ms"),
        "translation_final": run.get("translation_final"),
        "source_transcript": run.get("asr_final"),
        "audio_chunks": run.get("tts_audio_chunks"),
        "audio_bytes": run.get("tts_audio_bytes"),
        "fallback": run.get("fallback"),
        "usage": run.get("usage", {}),
        "voice_clone_status": run.get("voice_clone_status"),
    }


def _markdown(report: dict[str, Any]) -> str:
    lines = [
        "# LiveTranslate vs Modular controlled real smoke",
        "",
        "Mock latency is excluded. A missing real sample remains unavailable; "
        "no P95 is calculated.",
        "",
        "| Pipeline | Real samples | Status | Speech end → translation | "
        "Speech end → audio | Client playback | Audio chunks | Fallback |",
        "|---|---:|---|---:|---:|---:|---:|---|",
    ]
    for name in ("livetranslate", "modular"):
        item = report[name]
        lines.append(
            (
                "| {name} | {count} | {status} | {translation} | {audio} | "
                "{playback} | {chunks} | {fallback} |"
            ).format(
                name=name,
                count=item.get("sample_count", 0),
                status=item.get("status", "unavailable"),
                translation=_value(item.get("speech_end_to_first_translation_ms")),
                audio=_value(item.get("speech_end_to_first_audio_ms")),
                playback=_value(item.get("client_first_playback_ms")),
                chunks=_value(item.get("audio_chunks")),
                fallback=_value(item.get("fallback")),
            )
        )
    lines.extend(
        [
            "",
            "Subjective voice quality and clone similarity require confirmation by the user.",
            "",
        ]
    )
    return "\n".join(lines)


def _value(value: object) -> str:
    return "N/A" if value is None else str(value)


def main() -> int:
    report = build_report(_load_live(LIVE_REPORT), _load_modular(MODULAR_REPORT))
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "pipeline-comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    (OUTPUT_DIR / "pipeline-comparison.md").write_text(
        _markdown(report), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
