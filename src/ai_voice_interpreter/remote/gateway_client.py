from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from ..exceptions import GatewayError

logger = logging.getLogger(__name__)
UploadFinishedCallback = Callable[[], None]


@dataclass(frozen=True, slots=True)
class GatewayResponse:
    request_id: str
    recognized_text: str
    translated_text: str
    audio_id: str
    audio_url: str
    latency: dict[str, float]
    usage: dict[str, float | int]
    models: dict[str, str]
    provider_request_ids: dict[str, str]
    upload_and_processing_ms: float


@dataclass(slots=True)
class DownloadedAudio:
    path: Path
    size_bytes: int
    format: str
    download_ms: float


class _ObservedFile:
    def __init__(self, path: Path, on_finished: UploadFinishedCallback | None) -> None:
        self._handle = path.open("rb")
        self._on_finished = on_finished
        self._announced = False

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        if not chunk and not self._announced:
            self._announced = True
            if self._on_finished:
                self._on_finished()
        return chunk

    def close(self) -> None:
        self._handle.close()

    def __getattr__(self, name: str) -> object:
        return getattr(self._handle, name)


class GatewayClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float = 120.0,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def interpret(
        self,
        audio_path: Path,
        *,
        source_language: str = "zh",
        target_language: str = "en",
        voice: str | None = None,
        request_id: str | None = None,
        on_upload_finished: UploadFinishedCallback | None = None,
    ) -> GatewayResponse:
        if not audio_path.is_file():
            raise GatewayError(f"录音文件不存在：{audio_path}")
        observed = _ObservedFile(audio_path, on_upload_finished)
        fields: dict[str, str] = {
            "source_language": source_language,
            "target_language": target_language,
        }
        if voice:
            fields["voice"] = voice
        if request_id:
            fields["request_id"] = request_id
        started = time.perf_counter()
        try:
            with self._client() as client:
                response = client.post(
                    f"{self.base_url}/v1/interpret",
                    headers=self._headers,
                    data=fields,
                    files={"audio": ("recording.wav", observed, "audio/wav")},
                )
        except httpx.TimeoutException as exc:
            raise GatewayError("远程服务请求超时，请重新录音后重试。") from exc
        except httpx.HTTPError as exc:
            raise GatewayError("无法连接远程服务，请检查网络后重试。") from exc
        finally:
            observed.close()
        elapsed = (time.perf_counter() - started) * 1000
        payload = self._json_or_error(response)
        if response.status_code >= 400:
            self._raise_response_error(response.status_code, payload)
        try:
            result = GatewayResponse(
                request_id=str(payload["request_id"]),
                recognized_text=str(payload["recognized_text"]),
                translated_text=str(payload["translated_text"]),
                audio_id=str(payload["audio_id"]),
                audio_url=str(payload["audio_url"]),
                latency={key: float(value) for key, value in payload["latency"].items()},
                usage=dict(payload.get("usage", {})),
                models={key: str(value) for key, value in payload["models"].items()},
                provider_request_ids={
                    key: str(value)
                    for key, value in payload.get("provider_request_ids", {}).items()
                },
                upload_and_processing_ms=elapsed,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise GatewayError("远程服务返回格式不完整。") from exc
        if not result.recognized_text.strip() or not result.translated_text.strip():
            raise GatewayError("远程服务返回了空文本。", result.request_id)
        return result

    def download_audio(self, response: GatewayResponse, destination: Path) -> DownloadedAudio:
        url = self._safe_audio_url(response.audio_url)
        started = time.perf_counter()
        try:
            with self._client() as client:
                result = client.get(url, headers=self._headers)
        except httpx.TimeoutException as exc:
            raise GatewayError("下载语音超时，请重试。", response.request_id) from exc
        except httpx.HTTPError as exc:
            raise GatewayError("语音下载失败，请检查网络。", response.request_id) from exc
        if result.status_code >= 400:
            self._raise_response_error(result.status_code, self._json_or_error(result))
        content_type = result.headers.get("content-type", "").split(";", 1)[0].lower()
        if content_type not in {"audio/wav", "audio/x-wav", "audio/wave"}:
            raise GatewayError("远程服务未返回有效 WAV 音频。", response.request_id)
        invalid_header = (
            len(result.content) < 44
            or result.content[:4] != b"RIFF"
            or result.content[8:12] != b"WAVE"
        )
        if invalid_header:
            raise GatewayError("下载的语音文件格式无效。", response.request_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(result.content)
        return DownloadedAudio(
            path=destination,
            size_bytes=len(result.content),
            format="wav",
            download_ms=(time.perf_counter() - started) * 1000,
        )

    def _client(self) -> httpx.Client:
        return httpx.Client(timeout=self.timeout_seconds, transport=self._transport)

    def _safe_audio_url(self, audio_url: str) -> str:
        url = urljoin(f"{self.base_url}/", audio_url.lstrip("/"))
        expected = urlsplit(self.base_url)
        actual = urlsplit(url)
        if (actual.scheme, actual.netloc) != (expected.scheme, expected.netloc):
            raise GatewayError("远程服务返回了不安全的音频地址。")
        return url

    @staticmethod
    def _json_or_error(response: httpx.Response) -> dict[str, object]:
        try:
            payload = response.json()
        except (json.JSONDecodeError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _raise_response_error(status_code: int, payload: dict[str, object]) -> None:
        request_id = str(payload.get("request_id", "")) or None
        if status_code == 401:
            message = "客户端访问凭证无效。"
        elif status_code == 413:
            message = "录音文件过大。"
        elif status_code == 429:
            message = "请求过于频繁或并发已满。"
        elif status_code >= 500:
            message = "远程服务暂时不可用。"
        else:
            error = payload.get("error")
            detail = error.get("message") if isinstance(error, dict) else None
            message = str(detail or "远程请求失败。")
        if request_id:
            message = f"{message} request_id={request_id}"
        raise GatewayError(message, request_id)
