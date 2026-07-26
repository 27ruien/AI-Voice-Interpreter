from __future__ import annotations

import numpy as np
import pytest

from ai_voice_interpreter.meeting.audio_format import (
    StreamingInputAdapter,
    StreamingOutputAdapter,
)


@pytest.mark.parametrize("rate", [48000, 44100])
def test_stereo_device_input_becomes_16k_mono(rate: int) -> None:
    frames = rate
    phase = np.arange(frames) / rate
    stereo = np.column_stack((np.sin(phase * 1000), np.sin(phase * 1000))).astype(
        np.float32
    )
    adapter = StreamingInputAdapter(rate, 2)
    pcm = adapter.process(stereo) + adapter.finish()
    assert abs(len(pcm) // 2 - 16000) <= 2


def test_24k_mono_output_becomes_48k_stereo() -> None:
    adapter = StreamingOutputAdapter(48000, 2)
    pcm = (np.arange(2400, dtype=np.int16) - 1200).astype("<i2").tobytes()
    output = np.vstack((adapter.process(pcm), adapter.finish()))
    assert abs(len(output) - 4800) <= 2
    assert np.allclose(output[:, 0], output[:, 1])


def test_output_channels_above_two_are_zeroed() -> None:
    adapter = StreamingOutputAdapter(48000, 16)
    output = np.vstack((adapter.process(b"\x01\x00" * 1200), adapter.finish()))
    assert np.any(output[:, :2])
    assert not np.any(output[:, 2:])


def test_chunked_state_matches_single_stream() -> None:
    source = (np.sin(np.arange(44100) / 15) * 0.5).astype(np.float32)
    whole = StreamingInputAdapter(44100, 1)
    whole_pcm = whole.process(source) + whole.finish()
    chunked = StreamingInputAdapter(44100, 1)
    pieces = [chunked.process(chunk) for chunk in np.array_split(source, 137)]
    pieces.append(chunked.finish())
    chunked_pcm = b"".join(pieces)
    assert np.allclose(
        np.frombuffer(whole_pcm, dtype="<i2"),
        np.frombuffer(chunked_pcm, dtype="<i2"),
        atol=1,
    )


def test_incomplete_sample_is_held_and_counted() -> None:
    adapter = StreamingInputAdapter(48000, 2)
    assert adapter.process(b"\x01") == b""
    adapter.finish()
    assert adapter.metrics.incomplete_bytes == 1


def test_zero_input_and_no_overflow() -> None:
    adapter = StreamingInputAdapter(48000, 2)
    assert adapter.process(np.zeros((0, 2), dtype=np.float32)) == b""
    pcm = adapter.process(np.full((4800, 2), 10.0, dtype=np.float32)) + adapter.finish()
    values = np.frombuffer(pcm, dtype="<i2")
    assert values.max() <= 32767 and values.min() >= -32768


def test_reset_restarts_stream_state() -> None:
    source = np.ones((4800, 1), dtype=np.float32) * 0.1
    adapter = StreamingInputAdapter(48000, 1)
    first = adapter.process(source) + adapter.finish()
    adapter.reset()
    second = adapter.process(source) + adapter.finish()
    assert first == second


def test_long_stream_has_no_linear_frame_drift() -> None:
    adapter = StreamingInputAdapter(44100, 1)
    chunks = []
    for _ in range(100):
        chunks.append(adapter.process(np.zeros((4410, 1), dtype=np.float32)))
    chunks.append(adapter.finish())
    assert abs(sum(len(chunk) for chunk in chunks) // 2 - 160000) <= 2
