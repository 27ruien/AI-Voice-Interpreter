from __future__ import annotations

import pytest

from server.app.providers.livetranslate import LiveTranslateSessionOptions
from server.app.providers.mock_livetranslate import MockLiveTranslateUpstreamSession


@pytest.mark.anyio
async def test_mock_livetranslate_emits_complete_official_event_order() -> None:
    mock = MockLiveTranslateUpstreamSession(object(), LiveTranslateSessionOptions())
    await mock.send_audio(b"\x00\x00")
    await mock.finish()
    types = [event["type"] async for event in mock.events()]
    assert types == [
        "session.created",
        "session.updated",
        "conversation.item.input_audio_transcription.text",
        "conversation.item.input_audio_transcription.completed",
        "response.created",
        "response.output_item.added",
        "response.content_part.added",
        "response.audio_transcript.text",
        "response.audio.delta",
        "response.audio_transcript.done",
        "response.audio.done",
        "response.content_part.done",
        "response.output_item.done",
        "response.done",
        "session.finished",
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("text_revision", "response.audio_transcript.text"),
        ("source_unavailable", "error"),
        ("audio_invalid", "response.audio.delta"),
        ("voice_clone_failed", "error"),
        ("access_denied", "error"),
        ("unpurchased", "error"),
        ("quota_exhausted", "error"),
        ("multi_turn", "response.done"),
    ],
)
async def test_mock_livetranslate_scenarios(scenario: str, expected: str) -> None:
    mock = MockLiveTranslateUpstreamSession(
        object(), LiveTranslateSessionOptions(), scenario=scenario
    )
    await mock.finish()
    types = [event["type"] async for event in mock.events()]
    assert expected in types


@pytest.mark.anyio
async def test_mock_cancel_cleans_session() -> None:
    mock = MockLiveTranslateUpstreamSession(object(), LiveTranslateSessionOptions())
    await mock.cancel()
    assert mock.cancelled is True
