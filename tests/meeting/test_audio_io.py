from __future__ import annotations

import numpy as np

from ai_voice_interpreter.meeting.audio_io import DirectionalAudioIO
from ai_voice_interpreter.meeting.smoke import mock_route


class FakeStream:
    def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.kwargs = kwargs
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.started = False

    def close(self) -> None:
        self.closed = True


def audio_io(*, queue_max_chunks: int = 10) -> DirectionalAudioIO:
    route = mock_route()
    return DirectionalAudioIO(
        route.local_microphone,
        route.meeting_virtual_microphone_output,
        queue_max_chunks=queue_max_chunks,
        input_stream_factory=FakeStream,
        output_stream_factory=FakeStream,
    )


def test_callbacks_only_queue_and_actual_output_write_is_measured() -> None:
    stream = audio_io()
    stream.start()
    captured = np.ones((4800, 1), dtype=np.float32) * 0.05
    stream._input_callback(captured, len(captured), None, None)  # noqa: SLF001
    pcm = stream.read_input_pcm()
    assert pcm
    assert stream.metrics.input_frames == 4800

    translated = (np.ones(2400, dtype=np.int16) * 1000).astype("<i2").tobytes()
    stream.enqueue_output_pcm(translated)
    output = np.empty((2400, 2), dtype=np.float32)
    stream._output_callback(output, len(output), None, None)  # noqa: SLF001
    assert np.any(output)
    assert np.allclose(output[:, 0], output[:, 1])
    assert stream.metrics.first_output_write_at > 0
    stream.close()
    assert not stream.active


def test_input_backpressure_is_direction_local_and_cleanup_drains_queue() -> None:
    first = audio_io(queue_max_chunks=1)
    second = audio_io(queue_max_chunks=1)
    first.start()
    second.start()
    chunk = np.zeros((480, 1), dtype=np.float32)
    first._input_callback(chunk, len(chunk), None, None)  # noqa: SLF001
    first._input_callback(chunk, len(chunk), None, None)  # noqa: SLF001
    assert first.metrics.input_backpressure_count == 1
    assert second.metrics.input_backpressure_count == 0
    first.close()
    second.close()
    assert first.read_input_pcm(timeout=0.001) == b""
    assert second.read_input_pcm(timeout=0.001) == b""
