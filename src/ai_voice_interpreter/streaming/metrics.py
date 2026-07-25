from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field


@dataclass(slots=True)
class QueueMetrics:
    current: int = 0
    peak: int = 0
    warnings: int = 0

    def observe(self, depth: int, maximum: int) -> None:
        self.current = depth
        self.peak = max(self.peak, depth)
        if maximum and depth / maximum >= 0.8:
            self.warnings += 1


@dataclass(slots=True)
class TurnMetrics:
    asr_first_partial_ms: float = 0.0
    turn_finalize_ms: float = 0.0
    translation_first_token_ms: float = 0.0
    translation_final_ms: float = 0.0
    tts_first_audio_ms: float = 0.0
    server_time_to_first_audio_ms: float = 0.0
    client_first_playback_ms: float = 0.0
    end_to_end_ttfa_ms: float = 0.0
    turn_total_ms: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


@dataclass(slots=True)
class SessionMetrics:
    input_audio_bytes: int = 0
    websocket_sent_bytes: int = 0
    websocket_received_bytes: int = 0
    provider_calls: dict[str, int] = field(
        default_factory=lambda: {"asr": 0, "translation": 0, "tts": 0}
    )
    fallback_count: int = 0
    queue_peaks: dict[str, int] = field(default_factory=dict)


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(quantile * len(ordered)) - 1)
    return float(ordered[index])
