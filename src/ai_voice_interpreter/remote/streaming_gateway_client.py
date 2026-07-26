from __future__ import annotations

import json
import logging
import platform
import ssl
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import certifi
from websockets.sync.client import ClientConnection, connect

from .. import __version__
from ..exceptions import GatewayError
from ..streaming.protocol import PROTOCOL_VERSION, AudioInputSpec, SessionStart, new_id

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class StreamPacket:
    event: dict[str, Any] | None = None
    audio: bytes | None = None


class StreamingGatewayClient:
    """Blocking WSS transport; callers may send and receive from separate threads."""

    def __init__(
        self,
        base_url: str,
        token: str,
        timeout_seconds: float = 45.0,
        *,
        connect_factory: Callable[..., ClientConnection] = connect,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self._connect_factory = connect_factory
        self._connection: ClientConnection | None = None
        self.session_id: str | None = None
        self.request_id: str | None = None

    @property
    def websocket_url(self) -> str:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise GatewayError("AI_GATEWAY_BASE_URL 不是有效的 HTTP(S) 地址。")
        path = f"{parsed.path.rstrip('/')}/v1/stream"
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return urlunsplit((scheme, parsed.netloc, path, "", ""))

    def open(
        self,
        *,
        source_language: str = "zh",
        target_language: str = "en",
        voice: str | None = None,
        chunk_ms: int = 100,
        voice_mode: str = "standard",
        pipeline_provider: str | None = None,
        source_transcription_enabled: bool = True,
    ) -> dict[str, Any]:
        if self._connection is not None:
            raise GatewayError("流式连接已经打开。")
        self.request_id = new_id()
        start = SessionStart(
            request_id=self.request_id,
            source_language=source_language,
            target_language=target_language,
            mode="turn_stream",
            voice=voice,
            audio=AudioInputSpec("pcm_s16le", 16000, 1, chunk_ms),
            client_platform=f"macos-{platform.machine()}",
            app_version=__version__,
            pipeline_provider=pipeline_provider,
            voice_mode=voice_mode,
            source_transcription_enabled=source_transcription_enabled,
            protocol_version=PROTOCOL_VERSION,
        )
        try:
            connection = self._connect_factory(
                self.websocket_url,
                additional_headers={"Authorization": f"Bearer {self.token}"},
                ssl=self._ssl_context(),
                open_timeout=self.timeout_seconds,
                close_timeout=5,
                ping_interval=20,
                ping_timeout=20,
                max_size=2 * 1024 * 1024,
                max_queue=32,
            )
            connection.send(json.dumps(start.to_message(), ensure_ascii=False))
            raw = connection.recv(timeout=self.timeout_seconds)
        except Exception as exc:
            self.close()
            raise GatewayError(f"无法建立流式连接：{type(exc).__name__}") from exc
        if not isinstance(raw, str):
            connection.close()
            raise GatewayError("流式服务未返回 session.started。")
        event = self._parse_event(raw)
        if event.get("type") == "error":
            connection.close()
            raise GatewayError(self._safe_error_message(event), self.request_id)
        if event.get("type") != "session.started":
            connection.close()
            raise GatewayError("流式服务握手响应无效。", self.request_id)
        self._connection = connection
        self.session_id = str(event.get("session_id", "")) or None
        logger.info(
            "Streaming gateway connected session_id=%s protocol=%s",
            self.session_id,
            event.get("protocol_version"),
        )
        return event

    def send_audio(self, pcm: bytes) -> None:
        if not pcm or len(pcm) % 2:
            raise GatewayError("待发送音频必须是非空 16-bit PCM。", self.request_id)
        self._require_connection().send(pcm)

    def send_ping(self) -> None:
        self._require_connection().send(
            json.dumps({"type": "ping", "timestamp_ms": round(time.time() * 1000)})
        )

    def stop_session(self) -> None:
        connection = self._connection
        if connection is not None:
            connection.send(json.dumps({"type": "session.stop", "request_id": new_id()}))

    def receive(self, timeout: float | None = None) -> StreamPacket:
        try:
            raw = self._require_connection().recv(timeout=timeout)
        except TimeoutError:
            raise
        except Exception as exc:
            raise GatewayError(f"流式连接接收失败：{type(exc).__name__}", self.request_id) from exc
        if isinstance(raw, bytes):
            return StreamPacket(audio=raw)
        event = self._parse_event(raw)
        if event.get("type") == "error":
            raise GatewayError(self._safe_error_message(event), self.request_id)
        return StreamPacket(event=event)

    def packets(self, timeout: float | None = None) -> Iterator[StreamPacket]:
        while True:
            packet = self.receive(timeout)
            yield packet
            if packet.event and packet.event.get("type") == "session.completed":
                return

    def close(self) -> None:
        connection, self._connection = self._connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                logger.debug("Streaming connection close failed", exc_info=True)

    def _require_connection(self) -> ClientConnection:
        if self._connection is None:
            raise GatewayError("流式连接尚未建立。", self.request_id)
        return self._connection

    def _ssl_context(self) -> ssl.SSLContext | None:
        if self.websocket_url.startswith("wss://"):
            return ssl.create_default_context(cafile=certifi.where())
        return None

    @staticmethod
    def _parse_event(raw: str) -> dict[str, Any]:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise GatewayError("流式服务返回了无效 JSON。") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
            raise GatewayError("流式服务返回的控制消息不完整。")
        return payload

    @staticmethod
    def _safe_error_message(event: dict[str, Any]) -> str:
        code = str(event.get("code", "STREAM_ERROR"))
        message = str(event.get("message", "流式服务处理失败。"))
        return f"{message} code={code}"
