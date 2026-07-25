from __future__ import annotations

import argparse
import json
import logging
import tempfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from .streaming.metrics import percentile
from .streaming.mock_harness import build_mock_app, run_mock_turn

METRICS = (
    "asr_first_partial_ms",
    "turn_finalize_ms",
    "translation_first_token_ms",
    "tts_first_audio_ms",
    "client_first_playback_ms",
    "end_to_end_ttfa_ms",
)


def benchmark(runs: int) -> dict[str, Any]:
    logging.getLogger("server.app.streaming.session").setLevel(logging.WARNING)
    results: list[dict[str, Any]] = []
    with (
        tempfile.TemporaryDirectory(prefix="aivi-benchmark-") as directory,
        TestClient(build_mock_app(Path(directory))) as client,
    ):
        for _ in range(runs):
            results.append(run_mock_turn(client))
    successes = [result for result in results if result["success"]]
    report: dict[str, Any] = {
        "mode": "mock_streaming",
        "runs": runs,
        "success_rate": len(successes) / runs,
        "fallback_rate": sum(bool(result["fallback"]) for result in results) / runs,
        "metrics": {},
    }
    for name in METRICS:
        values = [float(result["metrics"].get(name, 0)) for result in successes]
        report["metrics"][name] = {
            "p50": round(percentile(values, 0.5), 3),
            "p95": round(percentile(values, 0.95), 3),
            "max": round(max(values, default=0.0), 3),
        }
    return report


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Mock Streaming Benchmark",
        "",
        "> 本报告仅测量本机 Mock 流式链路，不代表真实 DashScope 网络延迟。",
        "",
        f"- Runs: {report['runs']}",
        f"- Success rate: {report['success_rate']:.1%}",
        f"- Fallback rate: {report['fallback_rate']:.1%}",
        "",
        "| Metric | p50 (ms) | p95 (ms) | max (ms) |",
        "|---|---:|---:|---:|",
    ]
    for name, values in report["metrics"].items():
        lines.append(f"| {name} | {values['p50']} | {values['p95']} | {values['max']} |")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local mock streaming benchmark")
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument("--output-dir", type=Path, default=Path("benchmark-output"))
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    report = benchmark(args.runs)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "stream-benchmark.json"
    markdown_path = args.output_dir / "stream-benchmark.md"
    json_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["success_rate"] == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
