from __future__ import annotations

from server.app.streaming.vad import TurnVAD, VADEventType

SPEECH = b"\x01\x00" * 320
SILENCE = b"\0\0" * 320


def classifier(frame: bytes, _rate: int) -> bool:
    return any(frame)


def make_vad(**kwargs: int) -> TurnVAD:
    return TurnVAD(
        frame_ms=20,
        min_speech_ms=60,
        silence_ms=60,
        pre_roll_ms=40,
        max_turn_ms=200,
        classifier=classifier,
        **kwargs,
    )


def test_pure_silence_and_short_noise_make_no_turn() -> None:
    vad = make_vad()
    assert vad.feed(SILENCE * 5) == []
    assert vad.feed(SPEECH * 2 + SILENCE) == []
    assert vad.flush() == []


def test_normal_turn_has_preroll_and_silence_finalization() -> None:
    vad = make_vad()
    events = vad.feed(SILENCE * 2 + SPEECH * 4 + SILENCE * 3)
    assert [event.type for event in events].count(VADEventType.SPEECH_START) == 1
    assert events[0].audio.startswith(SILENCE)
    end = next(event for event in events if event.type == VADEventType.SPEECH_END)
    assert end.silence_ms == 60
    assert not end.forced


def test_max_turn_forces_split_and_multiple_turns_work() -> None:
    vad = make_vad()
    events = vad.feed(SPEECH * 14)
    assert any(event.type == VADEventType.SPEECH_END and event.forced for event in events)
    events += vad.feed(SILENCE * 3 + SPEECH * 3 + SILENCE * 3)
    # Forced continuation becomes a new turn, followed by the later natural turn.
    assert sum(event.type == VADEventType.SPEECH_START for event in events) == 3


def test_partial_frame_boundary_is_buffered() -> None:
    vad = make_vad()
    half = len(SPEECH) // 2
    assert vad.feed(SPEECH[:half]) == []
    assert vad.feed(SPEECH[half:] + SPEECH * 2)[0].type == VADEventType.SPEECH_START
