from __future__ import annotations

import re
import time

SENTENCE_BOUNDARY = re.compile(r"(?<=[,;:!?])\s+|(?<=[.!?])$")


class TTSTextSegmenter:
    def __init__(
        self,
        min_chars: int = 20,
        target_chars: int = 48,
        max_chars: int = 90,
        max_wait_ms: int = 300,
    ) -> None:
        if not 0 < min_chars <= target_chars <= max_chars:
            raise ValueError("Invalid text segmentation thresholds")
        self.min_chars = min_chars
        self.target_chars = target_chars
        self.max_chars = max_chars
        self.max_wait_ms = max_wait_ms
        self.pending = ""
        self._pending_since: float | None = None

    def feed(self, delta: str, *, now: float | None = None) -> list[str]:
        if not delta:
            return []
        clock = time.monotonic() if now is None else now
        if not self.pending:
            self._pending_since = clock
        self.pending += delta
        return self._extract(force_wait=False, now=clock)

    def poll(self, *, now: float | None = None) -> list[str]:
        clock = time.monotonic() if now is None else now
        waited = (
            self._pending_since is not None
            and (clock - self._pending_since) * 1000 >= self.max_wait_ms
        )
        return self._extract(force_wait=waited, now=clock)

    def flush(self) -> list[str]:
        if not self.pending:
            return []
        segment = self.pending
        self.pending = ""
        self._pending_since = None
        return [segment]

    def _extract(self, *, force_wait: bool, now: float) -> list[str]:
        segments: list[str] = []
        while self.pending:
            boundary = self._preferred_boundary()
            if boundary is None:
                if len(self.pending) >= self.max_chars:
                    boundary = self._word_boundary(self.max_chars)
                elif force_wait and len(self.pending) >= self.min_chars:
                    boundary = self._word_boundary(min(len(self.pending), self.target_chars))
                else:
                    break
            segment = self.pending[:boundary]
            if not segment:
                break
            segments.append(segment)
            self.pending = self.pending[boundary:]
            self._pending_since = now if self.pending else None
        return segments

    def _preferred_boundary(self) -> int | None:
        candidates = [
            match.end()
            for match in SENTENCE_BOUNDARY.finditer(self.pending)
            if match.end() >= self.min_chars
        ]
        if not candidates:
            return None
        within_target = [value for value in candidates if value <= self.target_chars]
        return max(within_target) if within_target else min(candidates)

    def _word_boundary(self, limit: int) -> int:
        if limit >= len(self.pending):
            return len(self.pending)
        boundary = self.pending.rfind(" ", 0, limit + 1)
        if boundary > 0:
            return boundary + 1
        next_space = self.pending.find(" ", limit)
        return next_space + 1 if next_space >= 0 else len(self.pending)
