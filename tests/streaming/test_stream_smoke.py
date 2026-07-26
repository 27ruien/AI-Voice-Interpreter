from __future__ import annotations

import pytest

from ai_voice_interpreter.config import AppConfig
from ai_voice_interpreter.exceptions import GatewayError
from ai_voice_interpreter.stream_smoke import StreamingSmokeRunner


class FailingClient:
    def send_audio(self, _pcm: bytes) -> None:
        raise ConnectionError("close frame")

    def close(self) -> None:
        return None


class DelayedReceiverError:
    def __init__(self, runner: StreamingSmokeRunner) -> None:
        self.runner = runner
        self.joined = False

    def join(self, timeout: float | None = None) -> None:
        assert timeout is not None and timeout <= 0.25
        self.joined = True
        self.runner._receiver_error = GatewayError(  # noqa: SLF001
            "模型访问被拒绝 code=ASR_STREAM_FAILED"
        )


def test_stream_smoke_prefers_structured_receiver_error_over_close_frame() -> None:
    runner = StreamingSmokeRunner(
        AppConfig(
            app_mode="real",
            interpreter_mode="remote_stream",
            ai_gateway_token="unit-test-token",
            network_timeout_seconds=1,
        )
    )
    runner.client = FailingClient()  # type: ignore[assignment]
    receiver = DelayedReceiverError(runner)
    try:
        with pytest.raises(GatewayError, match="ASR_STREAM_FAILED"):
            runner._send_audio_or_raise(b"\0\0", receiver)  # type: ignore[arg-type]  # noqa: SLF001
        assert receiver.joined
    finally:
        runner.close()


def test_stream_smoke_does_not_mask_send_error_without_receiver_error() -> None:
    runner = StreamingSmokeRunner(
        AppConfig(
            app_mode="real",
            interpreter_mode="remote_stream",
            ai_gateway_token="unit-test-token",
        )
    )
    runner.client = FailingClient()  # type: ignore[assignment]

    class Receiver:
        def join(self, timeout: float | None = None) -> None:
            del timeout

    try:
        with pytest.raises(ConnectionError, match="close frame"):
            runner._send_audio_or_raise(  # type: ignore[arg-type]  # noqa: SLF001
                b"\0\0", Receiver()
            )
    finally:
        runner.close()
