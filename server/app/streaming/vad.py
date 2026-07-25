from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from ai_voice_interpreter.streaming.protocol import ErrorCode, ProtocolError


class VADEventType(StrEnum):
    SPEECH_START = "speech_start"
    SPEECH_AUDIO = "speech_audio"
    SPEECH_END = "speech_end"


@dataclass(frozen=True, slots=True)
class VADEvent:
    type: VADEventType
    audio: bytes = b""
    speech_ms: int = 0
    silence_ms: int = 0
    forced: bool = False


class TurnVAD:
    def __init__(
        self,
        *,
        sample_rate: int = 16000,
        frame_ms: int = 20,
        aggressiveness: int = 2,
        min_speech_ms: int = 250,
        silence_ms: int = 650,
        pre_roll_ms: int = 200,
        max_turn_ms: int = 15000,
        classifier: Callable[[bytes, int], bool] | None = None,
    ) -> None:
        if frame_ms not in {10, 20, 30}:
            raise ValueError("WebRTC VAD frame must be 10, 20, or 30 ms")
        if classifier is None:
            import webrtcvad

            vad = webrtcvad.Vad(aggressiveness)
            classifier = vad.is_speech
        self.classifier = classifier
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.frame_bytes = sample_rate * frame_ms // 1000 * 2
        self.min_speech_frames = max(1, (min_speech_ms + frame_ms - 1) // frame_ms)
        self.silence_frames_needed = max(1, (silence_ms + frame_ms - 1) // frame_ms)
        self.pre_roll_frames = max(0, pre_roll_ms // frame_ms)
        self.max_turn_frames = max(1, max_turn_ms // frame_ms)
        self._remainder = bytearray()
        self._pre_roll: deque[bytes] = deque(maxlen=self.pre_roll_frames)
        self._candidate: list[bytes] = []
        self._active = False
        self._turn_frames = 0
        self._speech_frames = 0
        self._silence_frames = 0

    @property
    def active(self) -> bool:
        return self._active

    def feed(self, chunk: bytes) -> list[VADEvent]:
        if len(chunk) % 2:
            raise ProtocolError(ErrorCode.INVALID_AUDIO_FORMAT, "PCM Frame 必须按 16-bit 对齐。")
        self._remainder.extend(chunk)
        events: list[VADEvent] = []
        while len(self._remainder) >= self.frame_bytes:
            frame = bytes(self._remainder[: self.frame_bytes])
            del self._remainder[: self.frame_bytes]
            events.extend(self._process_frame(frame))
        return events

    def flush(self) -> list[VADEvent]:
        if not self._active:
            self._candidate.clear()
            self._remainder.clear()
            return []
        event = self._end_event(forced=True)
        self._remainder.clear()
        return [event]

    def _process_frame(self, frame: bytes) -> list[VADEvent]:
        is_speech = bool(self.classifier(frame, self.sample_rate))
        if not self._active:
            if is_speech:
                self._candidate.append(frame)
                if len(self._candidate) >= self.min_speech_frames:
                    audio = b"".join((*self._pre_roll, *self._candidate))
                    self._active = True
                    self._turn_frames = len(self._candidate)
                    self._speech_frames = len(self._candidate)
                    self._silence_frames = 0
                    self._candidate.clear()
                    self._pre_roll.clear()
                    return [
                        VADEvent(
                            VADEventType.SPEECH_START,
                            audio=audio,
                            speech_ms=self._speech_frames * self.frame_ms,
                        )
                    ]
            else:
                self._candidate.clear()
                self._pre_roll.append(frame)
            return []

        self._turn_frames += 1
        if is_speech:
            self._speech_frames += 1
            self._silence_frames = 0
        else:
            self._silence_frames += 1
        events = [
            VADEvent(
                VADEventType.SPEECH_AUDIO,
                audio=frame,
                speech_ms=self._speech_frames * self.frame_ms,
                silence_ms=self._silence_frames * self.frame_ms,
            )
        ]
        if self._silence_frames >= self.silence_frames_needed:
            events.append(self._end_event(forced=False))
        elif self._turn_frames >= self.max_turn_frames:
            events.append(self._end_event(forced=True))
        return events

    def _end_event(self, *, forced: bool) -> VADEvent:
        event = VADEvent(
            VADEventType.SPEECH_END,
            speech_ms=self._speech_frames * self.frame_ms,
            silence_ms=self._silence_frames * self.frame_ms,
            forced=forced,
        )
        self._active = False
        self._turn_frames = 0
        self._speech_frames = 0
        self._silence_frames = 0
        self._candidate.clear()
        self._pre_roll.clear()
        return event
