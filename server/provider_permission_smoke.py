from __future__ import annotations

import argparse
import asyncio
import json
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.app.config import ServerConfig
from server.app.providers.livetranslate import (
    LiveTranslateProviderError,
    LiveTranslateSessionOptions,
    LiveTranslateUpstreamSession,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Controlled LiveTranslate permission probe")
    parser.add_argument("--audio", type=Path, required=True)
    return parser


async def probe(
    audio_path: Path,
    *,
    config: ServerConfig | None = None,
    upstream_factory: Callable[..., LiveTranslateUpstreamSession] = (
        LiveTranslateUpstreamSession
    ),
) -> dict[str, Any]:
    config = config or ServerConfig.load()
    result: dict[str, Any] = {
        "success": False,
        "model": config.livetranslate_model,
        "session_id": None,
        "response_id": None,
        "event_id": None,
        "error_code": None,
        "message": None,
    }
    upstream = upstream_factory(
        config,
        LiveTranslateSessionOptions(
            source_language=config.livetranslate_source_language,
            target_language=config.livetranslate_target_language,
            voice_mode="standard",
            source_transcription_enabled=config.livetranslate_enable_source_transcription,
        ),
    )
    try:
        await upstream.start()
        result["session_id"] = upstream.session_id
        with wave.open(str(audio_path), "rb") as source:
            if (
                source.getframerate() != 16000
                or source.getnchannels() != 1
                or source.getsampwidth() != 2
            ):
                raise ValueError("Permission smoke WAV must be 16 kHz mono 16-bit PCM")
            frames = source.getframerate() // 10
            deadline = time.monotonic()
            while pcm := source.readframes(frames):
                await upstream.send_audio(pcm)
                deadline += 0.1
                await asyncio.sleep(max(0.0, deadline - time.monotonic()))
        await upstream.finish()

        async def wait_finished() -> None:
            async for event in upstream.events():
                result["event_id"] = event.get("event_id") or result["event_id"]
                if event.get("type") == "error":
                    raise LiveTranslateProviderError.from_event(event)
                if event.get("type") == "response.created":
                    response = event.get("response")
                    if isinstance(response, dict):
                        result["response_id"] = response.get("id")
                if event.get("type") == "session.finished":
                    result["success"] = True
                    result["message"] = "session.updated and session.finished received"

        await asyncio.wait_for(
            wait_finished(), timeout=config.livetranslate_session_finish_timeout_seconds
        )
    except LiveTranslateProviderError as exc:
        result["error_code"] = exc.code
        result["message"] = exc.message
        result["event_id"] = exc.event_id or result["event_id"]
        result["response_id"] = exc.response_id or result["response_id"]
    except TimeoutError:
        result["error_code"] = "FINISH_TIMEOUT"
        result["message"] = "session.finished timeout"
    except Exception as exc:
        result["error_code"] = type(exc).__name__
        result["message"] = str(exc)[:300]
    finally:
        await upstream.cancel()
    return result


def main() -> int:
    args = _parser().parse_args()
    result = asyncio.run(probe(args.audio))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
