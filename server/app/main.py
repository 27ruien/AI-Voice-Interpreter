from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from ai_voice_interpreter.logging_config import configure_logging
from ai_voice_interpreter.pipeline import InterpreterPipeline

from .audio_store import AudioStore, validate_wav
from .auth import BearerTokenAuth
from .config import ServerConfig
from .errors import GatewayHTTPError
from .pipeline import build_server_pipeline, generated_audio_size

logger = logging.getLogger(__name__)


class ConcurrencyGate:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.active = 0
        self._lock = asyncio.Lock()

    async def enter(self) -> None:
        async with self._lock:
            if self.active >= self.limit:
                raise GatewayHTTPError(429, "concurrency_limit", "服务器并发已满，请稍后重试。")
            self.active += 1

    async def leave(self) -> None:
        async with self._lock:
            self.active = max(0, self.active - 1)


def create_app(
    config: ServerConfig | None = None,
    *,
    pipeline: InterpreterPipeline | Any | None = None,
) -> FastAPI:
    settings = config or ServerConfig.load()
    configure_logging(settings.log_level)
    store = AudioStore(settings.temp_audio_dir, settings.audio_ttl_seconds)
    processor = pipeline or build_server_pipeline(settings)
    auth = BearerTokenAuth(settings.client_test_token)
    gate = ConcurrencyGate(settings.max_concurrent_requests)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(_cleanup_loop(store))
        yield
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    app = FastAPI(
        title="AI Voice Interpreter Gateway",
        version="1.0.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.config = settings
    app.state.audio_store = store
    app.state.pipeline = processor
    app.state.gate = gate

    @app.middleware("http")
    async def request_context(request: Request, call_next: Any) -> Any:
        request.state.request_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(GatewayHTTPError)
    async def gateway_error_handler(request: Request, exc: GatewayHTTPError) -> JSONResponse:
        return _error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "Unhandled gateway error request_id=%s type=%s",
            request.state.request_id,
            type(exc).__name__,
        )
        return _error_response(request, 500, "internal_error", "服务器暂时不可用。")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "service": "ai-voice-interpreter-gateway",
            "mode": "real",
        }

    @app.get("/readyz")
    async def readyz() -> dict[str, object]:
        writable = _directory_writable(settings.temp_audio_dir)
        if not writable:
            raise GatewayHTTPError(503, "not_ready", "临时音频目录不可写。")
        return {
            "status": "ready",
            "configuration": "loaded",
            "api_key": "configured",
            "providers": "initialized",
            "temp_directory": "writable",
        }

    @app.post("/v1/interpret", dependencies=[Depends(auth)])
    async def interpret(
        request: Request,
        audio: Annotated[UploadFile, File()],
        source_language: Annotated[str, Form()] = "zh",
        target_language: Annotated[str, Form()] = "en",
        voice: Annotated[str | None, Form()] = None,
        request_id: Annotated[str | None, Form()] = None,
    ) -> dict[str, object]:
        if request_id:
            try:
                request.state.request_id = str(uuid.UUID(request_id))
            except ValueError as exc:
                raise GatewayHTTPError(
                    400, "invalid_request_id", "request_id 必须是 UUID。"
                ) from exc
        if source_language != "zh" or target_language != "en":
            raise GatewayHTTPError(400, "unsupported_language", "当前仅支持中文到英文。")
        input_path = store.new_input_path()
        await gate.enter()
        try:
            await _save_upload(audio, input_path, settings.max_upload_mb)
            metadata = validate_wav(input_path)
            logger.info(
                "Interpret started request_id=%s audio_bytes=%d duration_seconds=%.2f",
                request.state.request_id,
                input_path.stat().st_size,
                metadata.duration_seconds,
            )
            result = await asyncio.to_thread(processor.process, input_path)
            if result.error:
                raise GatewayHTTPError(502, "provider_error", result.error)
            if not result.recognized_text.strip():
                raise GatewayHTTPError(502, "empty_asr", "ASR 未返回有效文本。")
            if not result.translated_text.strip():
                raise GatewayHTTPError(502, "empty_translation", "翻译未返回有效文本。")
            generated = result.generated_audio_path
            if generated is None or generated_audio_size(generated) < 44:
                raise GatewayHTTPError(502, "invalid_tts", "TTS 未返回有效音频。")
            audio_id, published = store.publish(generated)
            logger.info(
                "Interpret completed request_id=%s audio_id=%s audio_bytes=%d total_ms=%.1f",
                request.state.request_id,
                audio_id,
                published.stat().st_size,
                result.total_latency_ms,
            )
            return {
                "request_id": request.state.request_id,
                "recognized_text": result.recognized_text,
                "translated_text": result.translated_text,
                "audio_id": audio_id,
                "audio_url": f"/v1/audio/{audio_id}",
                "latency": {
                    "asr_ms": result.asr_latency_ms,
                    "translation_ms": result.translation_latency_ms,
                    "tts_ms": result.tts_latency_ms,
                    "total_ms": result.total_latency_ms,
                },
                "usage": {
                    "input_audio_seconds": round(metadata.duration_seconds, 3),
                    "translated_characters": len(result.translated_text),
                    "tts_characters": len(result.translated_text),
                },
                "models": {
                    **result.models,
                    "voice": voice or settings.cloned_voice_id or settings.tts_voice,
                },
                "provider_request_ids": result.request_ids,
            }
        finally:
            input_path.unlink(missing_ok=True)
            await audio.close()
            await gate.leave()

    @app.get("/v1/audio/{audio_id}", dependencies=[Depends(auth)])
    async def audio_download(audio_id: str) -> FileResponse:
        path = store.resolve(audio_id)
        if path is None:
            raise GatewayHTTPError(404, "audio_not_found", "音频不存在或已过期。")
        return FileResponse(
            path,
            media_type="audio/wav",
            filename=f"interpretation-{audio_id}.wav",
        )

    return app


async def _save_upload(upload: UploadFile, path: Path, max_upload_mb: int) -> None:
    maximum = max_upload_mb * 1024 * 1024
    total = 0
    try:
        with path.open("xb") as output:
            while chunk := await upload.read(1024 * 1024):
                total += len(chunk)
                if total > maximum:
                    raise GatewayHTTPError(413, "upload_too_large", "录音文件超过大小限制。")
                output.write(chunk)
    except GatewayHTTPError:
        raise
    except OSError as exc:
        raise GatewayHTTPError(500, "upload_storage_error", "无法保存临时录音。") from exc
    if total == 0:
        raise GatewayHTTPError(400, "empty_upload", "上传文件为空。")


async def _cleanup_loop(store: AudioStore) -> None:
    while True:
        await asyncio.sleep(min(30, max(1, store.ttl_seconds)))
        deleted = store.cleanup_expired()
        if deleted:
            logger.info("Expired TTS audio deleted count=%d", deleted)


def _directory_writable(directory: Path) -> bool:
    probe = directory / f".ready-{uuid.uuid4()}"
    try:
        probe.write_bytes(b"ok")
        return True
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)


def _error_response(
    request: Request,
    status_code: int,
    code: str,
    message: str,
) -> JSONResponse:
    request_id = getattr(request.state, "request_id", str(uuid.uuid4()))
    return JSONResponse(
        status_code=status_code,
        content={
            "request_id": request_id,
            "error": {"code": code, "message": message},
        },
        headers={"X-Request-ID": request_id},
    )
