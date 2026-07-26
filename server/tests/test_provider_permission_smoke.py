from __future__ import annotations

import asyncio
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from server.app.config import ServerConfig
from server.app.providers.livetranslate import PCMOutputSpec
from server.provider_permission_smoke import probe


class FakePermissionUpstream:
    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.session_id = "permission-session"
        self.model = "mock-live"
        self.output_spec = PCMOutputSpec(24000)
        self.audio_queue_peak = 0
        self.output_queue_peak = 0
        self.sent = 0
        self.cancelled = False

    async def start(self) -> None:
        return None

    async def send_audio(self, pcm: bytes) -> None:
        self.sent += len(pcm)

    async def finish(self) -> None:
        return None

    async def cancel(self) -> None:
        self.cancelled = True

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "response.created",
            "event_id": "permission-response-event",
            "response": {"id": "permission-response"},
        }
        yield {"type": "session.finished", "event_id": "permission-finished"}


def test_permission_probe_uses_realtime_chunks_without_real_api(tmp_path: Path) -> None:
    audio = tmp_path / "probe.wav"
    with wave.open(str(audio), "wb") as output:
        output.setframerate(16000)
        output.setnchannels(1)
        output.setsampwidth(2)
        output.writeframes(b"\x00\x00" * 1600)
    instance: FakePermissionUpstream | None = None

    def factory(*args: object, **kwargs: object) -> FakePermissionUpstream:
        nonlocal instance
        instance = FakePermissionUpstream(*args, **kwargs)
        return instance

    config = ServerConfig(
        dashscope_api_key="fake",
        dashscope_workspace_id="workspace",
        dashscope_native_base_url="https://example.test/api/v1",
        client_test_token="token",
    )
    result = asyncio.run(probe(audio, config=config, upstream_factory=factory))
    assert result["success"] is True
    assert result["session_id"] == "permission-session"
    assert result["response_id"] == "permission-response"
    assert instance is not None and instance.sent == 3200
    assert instance.cancelled is True
