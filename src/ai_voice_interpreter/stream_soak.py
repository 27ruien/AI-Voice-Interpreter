from __future__ import annotations

import argparse
import gc
import json
import logging
import tempfile
import threading
import time
import tracemalloc
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from .streaming.mock_harness import build_mock_app, run_mock_turn


def run_soak(minutes: float, interval_seconds: float) -> dict[str, Any]:
    logging.getLogger("server.app.streaming.session").setLevel(logging.WARNING)
    duration = minutes * 60
    started = time.monotonic()
    baseline_threads = threading.active_count()
    samples: list[int] = []
    runs = 0
    failures = 0
    tracemalloc.start()
    with tempfile.TemporaryDirectory(prefix="aivi-soak-") as directory:
        temp_dir = Path(directory)
        with TestClient(build_mock_app(temp_dir)) as client:
            while time.monotonic() - started < duration:
                try:
                    result = run_mock_turn(client)
                    failures += int(not result["success"])
                except Exception:
                    failures += 1
                runs += 1
                if runs % 10 == 0:
                    gc.collect()
                    current, _peak = tracemalloc.get_traced_memory()
                    samples.append(current)
                remaining = duration - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(min(interval_seconds, remaining))
        residual_files = [str(path.relative_to(temp_dir)) for path in temp_dir.rglob("*")]
    gc.collect()
    current_bytes, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    final_threads = threading.active_count()
    first_window = samples[: max(1, len(samples) // 5)]
    last_window = samples[-max(1, len(samples) // 5) :]
    first_average = sum(first_window) / len(first_window) if first_window else current_bytes
    last_average = sum(last_window) / len(last_window) if last_window else current_bytes
    growth_bytes = max(0.0, last_average - first_average)
    memory_stable = growth_bytes <= 8 * 1024 * 1024
    threads_stable = final_threads <= baseline_threads + 1
    return {
        "mode": "mock_streaming",
        "requested_minutes": minutes,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "runs": runs,
        "failures": failures,
        "success_rate": (runs - failures) / runs if runs else 0.0,
        "memory": {
            "current_bytes": current_bytes,
            "peak_bytes": peak_bytes,
            "window_growth_bytes": round(growth_bytes),
            "stable": memory_stable,
        },
        "threads": {
            "baseline": baseline_threads,
            "final": final_threads,
            "stable": threads_stable,
        },
        "residual_temp_files": residual_files,
        "passed": bool(
            runs and not failures and memory_stable and threads_stable and not residual_files
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Mock WSS streaming stability soak")
    parser.add_argument("--minutes", type=float, default=30)
    parser.add_argument("--interval-seconds", type=float, default=0.5)
    parser.add_argument("--json-report", type=Path, default=Path("soak-output/stream-soak.json"))
    args = parser.parse_args()
    if args.minutes <= 0 or args.interval_seconds < 0:
        parser.error("duration must be positive and interval non-negative")
    report = run_soak(args.minutes, args.interval_seconds)
    args.json_report.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(report, indent=2)
    args.json_report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
