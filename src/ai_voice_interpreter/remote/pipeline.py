from __future__ import annotations

import logging
import shutil
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

from ..exceptions import InterpreterError
from ..models import PipelineResult, ProcessingStatus
from .gateway_client import GatewayClient

logger = logging.getLogger(__name__)


class RemoteInterpreterPipeline:
    def __init__(
        self,
        client: GatewayClient,
        source_language: str = "zh",
        target_language: str = "en",
        voice: str | None = None,
        *,
        output_dir: Path | None = None,
        keep_temp_audio: bool = False,
    ) -> None:
        self.client = client
        self.source_language = source_language
        self.target_language = target_language
        self.voice = voice
        self._owned_dir = output_dir is None
        self.output_dir = output_dir or Path(tempfile.mkdtemp(prefix="aivi-remote-"))
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.keep_temp_audio = keep_temp_audio

    def process(
        self,
        audio_path: Path,
        on_status: Callable[[ProcessingStatus], None] | None = None,
    ) -> PipelineResult:
        notify = on_status or (lambda _status: None)
        started = time.perf_counter()
        result = PipelineResult(providers={"gateway": "remote"})
        try:
            notify(ProcessingStatus.UPLOADING)
            gateway = self.client.interpret(
                audio_path,
                source_language=self.source_language,
                target_language=self.target_language,
                voice=self.voice,
                on_upload_finished=lambda: notify(ProcessingStatus.SERVER_PROCESSING),
            )
            result.gateway_request_id = gateway.request_id
            result.recognized_text = gateway.recognized_text
            result.translated_text = gateway.translated_text
            result.asr_latency_ms = gateway.latency.get("asr_ms", 0.0)
            result.translation_latency_ms = gateway.latency.get("translation_ms", 0.0)
            result.tts_latency_ms = gateway.latency.get("tts_ms", 0.0)
            result.models.update(gateway.models)
            result.request_ids.update(gateway.provider_request_ids)
            result.network_latency_ms["upload_and_processing_ms"] = (
                gateway.upload_and_processing_ms
            )
            notify(ProcessingStatus.DOWNLOADING)
            destination = self.output_dir / f"{gateway.audio_id}.wav"
            downloaded = self.client.download_audio(gateway, destination)
            result.generated_audio_path = downloaded.path
            result.network_latency_ms["download_ms"] = downloaded.download_ms
            logger.info(
                "Remote pipeline audio downloaded request_id=%s bytes=%d path=%s",
                gateway.request_id,
                downloaded.size_bytes,
                downloaded.path,
            )
        except InterpreterError as exc:
            result.error = str(exc)
            logger.warning("Remote pipeline failed message=%s", exc)
        except Exception as exc:
            result.error = f"远程处理失败：{exc}"
            logger.exception("Remote pipeline failed unexpectedly")
        finally:
            result.total_latency_ms = (time.perf_counter() - started) * 1000
        return result

    def cleanup(self) -> None:
        if self._owned_dir and not self.keep_temp_audio:
            shutil.rmtree(self.output_dir, ignore_errors=True)
