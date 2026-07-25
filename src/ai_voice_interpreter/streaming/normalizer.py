from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NormalizedDelta:
    text: str
    delta: str
    revised: bool = False


class DeltaNormalizer:
    """Normalize true deltas and cumulative provider chunks without duplication."""

    def __init__(self, *, cumulative: bool | None = None) -> None:
        self.cumulative = cumulative
        self.text = ""
        self._last_chunk = ""

    def push(self, chunk: str) -> NormalizedDelta:
        if not chunk or chunk == self._last_chunk:
            return NormalizedDelta(self.text, "")
        self._last_chunk = chunk
        if self.cumulative is False:
            self.text += chunk
            return NormalizedDelta(self.text, chunk)
        if chunk.startswith(self.text):
            delta = chunk[len(self.text) :]
            self.text = chunk
            return NormalizedDelta(self.text, delta)
        if self.cumulative is True:
            common = _common_prefix_length(self.text, chunk)
            revised = common < len(self.text)
            delta = chunk[common:] if not revised else ""
            self.text = chunk
            return NormalizedDelta(self.text, delta, revised)
        if self.text.startswith(chunk):
            return NormalizedDelta(self.text, "")
        self.text += chunk
        return NormalizedDelta(self.text, chunk)


def _common_prefix_length(left: str, right: str) -> int:
    length = min(len(left), len(right))
    for index in range(length):
        if left[index] != right[index]:
            return index
    return length
