from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import ssl
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import certifi
from websockets.asyncio.client import ClientConnection, connect

from ..config import ServerConfig

logger = logging.getLogger(__name__)


class LiveTranslateProviderError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        event_id: str | None = None,
        session_id: str | None = None,
        response_id: str | None = None,
        param: str | None = None,
    ) -> None:
        super().__init__(f"{message} code={code}")
        self.code = code
        self.message = message
        self.event_id = event_id
        self.session_id = session_id
        self.response_id = response_id
        self.param = param

    @classmethod
    def from_event(cls, event: dict[str, Any]) -> LiveTranslateProviderError:
        error = event.get("error")
        details = error if isinstance(error, dict) else {}
        return cls(
            str(details.get("code") or details.get("type") or "UPSTREAM_ERROR"),
            str(details.get("message") or "LiveTranslate 返回错误。"),
            event_id=_optional_text(event.get("event_id")),
            session_id=_optional_text(event.get("session_id")),
            response_id=_optional_text(event.get("response_id")),
            param=_optional_text(details.get("param")),
        )


@dataclass(frozen=True, slots=True)
class LiveTranslateSessionOptions:
    source_language: str = "zh"
    target_language: str = "en"
    voice_mode: str = "standard"
    source_transcription_enabled: bool = True
    voice: str | None = None
    session_role: str | None = None


@dataclass(frozen=True, slots=True)
class PCMOutputSpec:
    sample_rate: int
    channels: int = 1
    sample_width: int = 2
    format: str = "pcm_s16le"


class TranscriptNormalizer:
    """Normalize LiveTranslate's confirmed text plus replaceable stash."""

    def __init__(self) -> None:
        self.confirmed = ""
        self.stash = ""
        self.display = ""
        self.final = ""
        self.finalized = False

    def update(self, confirmed: object, stash: object) -> str | None:
        if self.finalized:
            return None
        confirmed_text = str(confirmed or "")
        stash_text = str(stash or "")
        display = confirmed_text + stash_text
        self.confirmed = confirmed_text
        self.stash = stash_text
        if not display or display == self.display:
            return None
        self.display = display
        return display

    def complete(self, text: object) -> str | None:
        if self.finalized:
            return None
        final = str(text or "").strip()
        if not final:
            final = (self.confirmed + self.stash).strip()
        self.finalized = True
        self.final = final
        self.display = final
        return final or None


def build_livetranslate_endpoint(workspace_id: str, model: str) -> str:
    workspace = workspace_id.strip()
    selected_model = model.strip()
    if not workspace:
        raise LiveTranslateProviderError("CONFIG_MISSING", "缺少 DASHSCOPE_WORKSPACE_ID。")
    if not selected_model:
        raise LiveTranslateProviderError("CONFIG_MISSING", "缺少 LIVETRANSLATE_MODEL。")
    return (
        f"wss://{workspace}.cn-beijing.maas.aliyuncs.com/api-ws/v1/realtime?"
        f"{urlencode({'model': selected_model})}"
    )


def authorization_headers(api_key: str) -> dict[str, str]:
    key = api_key.strip()
    if not key:
        raise LiveTranslateProviderError("CONFIG_MISSING", "缺少 DASHSCOPE_API_KEY。")
    return {"Authorization": f"Bearer {key}"}


def build_session_update(
    config: ServerConfig,
    options: LiveTranslateSessionOptions,
) -> dict[str, Any]:
    if (options.source_language, options.target_language) not in {
        ("zh", "en"),
        ("en", "zh"),
    }:
        raise LiveTranslateProviderError(
            "UNSUPPORTED_LANGUAGE", "当前 LiveTranslate 仅支持中文和英文双向翻译。"
        )
    if options.voice_mode not in {"standard", "clone_once"}:
        raise LiveTranslateProviderError("INVALID_VOICE_MODE", "无效的声音模式。")
    clone_enabled = options.voice_mode == "clone_once" or (
        options.voice_mode == "standard"
        and config.livetranslate_enable_voice_clone
        and options.session_role != "remote_to_local"
    )
    voice = "default" if clone_enabled else (options.voice or config.livetranslate_voice)
    if clone_enabled and config.livetranslate_voice_clone_frequency != "once":
        raise LiveTranslateProviderError(
            "INVALID_VOICE_CLONE_CONFIG", "本轮声音复刻频率仅允许 once。"
        )
    session: dict[str, Any] = {
        "modalities": list(config.livetranslate_output_modalities),
        "voice": voice,
        "sample_rate": config.stream_audio_sample_rate,
        "input_audio_format": "pcm",
        "output_audio_format": "pcm",
        "translation": {"language": options.target_language},
        "enable_voice_clone": clone_enabled,
    }
    source_transcription = (
        config.livetranslate_enable_source_transcription
        and options.source_transcription_enabled
    )
    if source_transcription:
        session["input_audio_transcription"] = {
            "model": config.livetranslate_source_asr_model,
            "language": options.source_language,
        }
    if config.livetranslate_hotwords:
        session["translation"]["corpus"] = {
            "phrases": dict(config.livetranslate_hotwords)
        }
    if clone_enabled:
        session["voice_clone_options"] = {"frequency": "once"}
    return {
        "event_id": _event_id(),
        "type": "session.update",
        "session": session,
    }


def build_audio_append(pcm: bytes) -> dict[str, str]:
    if not pcm:
        raise LiveTranslateProviderError("EMPTY_AUDIO", "LiveTranslate 音频块不能为空。")
    if len(pcm) % 2:
        raise LiveTranslateProviderError("INVALID_AUDIO", "PCM 音频字节长度必须为偶数。")
    return {
        "event_id": _event_id(),
        "type": "input_audio_buffer.append",
        "audio": base64.b64encode(pcm).decode("ascii"),
    }


def decode_audio_delta(value: object) -> bytes:
    if not isinstance(value, str) or not value:
        raise LiveTranslateProviderError("INVALID_AUDIO", "上游音频 Delta 为空。")
    try:
        pcm = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise LiveTranslateProviderError(
            "INVALID_AUDIO", "上游音频 Delta 不是有效 Base64。"
        ) from exc
    if not pcm or len(pcm) % 2:
        raise LiveTranslateProviderError("INVALID_AUDIO", "上游 PCM 音频无效。")
    return pcm


def output_pcm_spec(output_audio_format: object) -> PCMOutputSpec:
    # The protocol requests generic ``pcm`` while current server responses may
    # normalize that value to ``pcm24``. Both represent 24 kHz, 16-bit mono PCM
    # for LiveTranslate output.
    if output_audio_format in {"pcm", "pcm24"}:
        return PCMOutputSpec(sample_rate=24000)
    raise LiveTranslateProviderError(
        "UNSUPPORTED_OUTPUT_AUDIO_FORMAT",
        f"LiveTranslate 返回不支持的输出格式：{output_audio_format or 'missing'}。",
    )


class LiveTranslateUpstreamSession:
    active_connections = 0

    def __init__(
        self,
        config: ServerConfig,
        options: LiveTranslateSessionOptions,
        *,
        connect_factory: Callable[..., Any] = connect,
    ) -> None:
        self.config = config
        self.options = options
        self._connect_factory = connect_factory
        self._connection: ClientConnection | Any | None = None
        self._audio_queue: asyncio.Queue[bytes | None] = asyncio.Queue(
            config.livetranslate_audio_queue_max_chunks
        )
        self._event_queue: asyncio.Queue[dict[str, Any] | Exception | None] = asyncio.Queue(
            config.livetranslate_audio_queue_max_chunks
        )
        self._sender_task: asyncio.Task[None] | None = None
        self._receiver_task: asyncio.Task[None] | None = None
        self.session_id: str | None = None
        self.model: str = config.livetranslate_model
        self.output_spec: PCMOutputSpec | None = None
        self.audio_queue_peak = 0
        self.output_queue_peak = 0
        self._closed = False
        self._finished = False
        self._counted_active = False

    @property
    def endpoint(self) -> str:
        return build_livetranslate_endpoint(
            self.config.dashscope_workspace_id, self.config.livetranslate_model
        )

    async def start(self) -> None:
        context = ssl.create_default_context(cafile=certifi.where())
        try:
            self._connection = await self._connect_factory(
                self.endpoint,
                additional_headers=authorization_headers(self.config.dashscope_api_key),
                ssl=context,
                open_timeout=self.config.livetranslate_connect_timeout_seconds,
                close_timeout=5,
                ping_interval=20,
                ping_timeout=20,
                max_size=4 * 1024 * 1024,
                max_queue=32,
            )
            self._counted_active = True
            type(self).active_connections += 1
            created = await self._receive_json(
                self.config.livetranslate_connect_timeout_seconds
            )
            self._raise_if_error(created)
            if created.get("type") != "session.created":
                raise LiveTranslateProviderError(
                    "INVALID_HANDSHAKE", "LiveTranslate 未返回 session.created。"
                )
            await self._send_json(build_session_update(self.config, self.options))
            updated = await self._receive_json(
                self.config.livetranslate_connect_timeout_seconds
            )
            self._raise_if_error(updated)
            if updated.get("type") != "session.updated":
                raise LiveTranslateProviderError(
                    "INVALID_SESSION_UPDATE", "LiveTranslate 未返回 session.updated。"
                )
            session = updated.get("session")
            if not isinstance(session, dict):
                raise LiveTranslateProviderError(
                    "INVALID_SESSION_UPDATE", "session.updated 缺少 Session 配置。"
                )
            self.session_id = _optional_text(session.get("id"))
            self.model = _optional_text(session.get("model")) or self.model
            if not self.session_id:
                raise LiveTranslateProviderError(
                    "INVALID_SESSION_UPDATE", "session.updated 缺少 Session ID。"
                )
            self.output_spec = output_pcm_spec(session.get("output_audio_format"))
            self._sender_task = asyncio.create_task(self._sender_loop())
            self._receiver_task = asyncio.create_task(self._receiver_loop())
        except Exception:
            await self.cancel()
            raise

    async def send_audio(self, pcm: bytes) -> None:
        if self._closed or self._connection is None:
            raise LiveTranslateProviderError("CONNECTION_CLOSED", "上游连接已关闭。")
        if not pcm or len(pcm) % 2:
            raise LiveTranslateProviderError("INVALID_AUDIO", "PCM 音频块无效。")
        try:
            self._audio_queue.put_nowait(pcm)
        except asyncio.QueueFull as exc:
            raise LiveTranslateProviderError(
                "AUDIO_QUEUE_FULL", "LiveTranslate 音频队列已满。"
            ) from exc
        depth = self._audio_queue.qsize()
        self.audio_queue_peak = max(self.audio_queue_peak, depth)
        if depth / self._audio_queue.maxsize >= 0.8:
            logger.warning(
                "LiveTranslate audio queue pressure session_id=%s depth=%d max=%d",
                self.session_id,
                depth,
                self._audio_queue.maxsize,
            )

    async def finish(self) -> None:
        if self._closed or self._connection is None:
            return
        if self._sender_task is not None:
            await self._audio_queue.put(None)
            await self._sender_task
            self._sender_task = None
        await self._send_json({"event_id": _event_id(), "type": "session.finish"})

    async def cancel(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in (self._sender_task, self._receiver_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._sender_task, self._receiver_task):
            if task is not None:
                with suppress(asyncio.CancelledError, Exception):
                    await task
        if self._connection is not None:
            with suppress(Exception):
                await self._connection.close()
        self._connection = None
        if self._counted_active:
            type(self).active_connections = max(0, type(self).active_connections - 1)
            self._counted_active = False
        _drain_queue(self._audio_queue)
        _drain_queue(self._event_queue)

    async def events(self) -> AsyncIterator[dict[str, Any]]:
        while True:
            item = await self._event_queue.get()
            if item is None:
                return
            if isinstance(item, Exception):
                raise item
            yield item
            if item.get("type") == "session.finished":
                self._finished = True
                return

    async def _sender_loop(self) -> None:
        while True:
            pcm = await self._audio_queue.get()
            if pcm is None:
                return
            await self._send_json(build_audio_append(pcm))

    async def _receiver_loop(self) -> None:
        try:
            assert self._connection is not None
            async for raw in self._connection:
                if not isinstance(raw, str):
                    raise LiveTranslateProviderError(
                        "INVALID_UPSTREAM_FRAME", "LiveTranslate 返回了非 JSON Frame。"
                    )
                event = _parse_json_event(raw)
                try:
                    self._event_queue.put_nowait(event)
                except asyncio.QueueFull as exc:
                    raise LiveTranslateProviderError(
                        "OUTPUT_QUEUE_FULL", "LiveTranslate 输出队列已满。"
                    ) from exc
                self.output_queue_peak = max(
                    self.output_queue_peak, self._event_queue.qsize()
                )
                if event.get("type") == "session.finished":
                    return
            if not self._finished and not self._closed:
                raise LiveTranslateProviderError(
                    "UPSTREAM_DISCONNECTED", "LiveTranslate 上游连接提前断开。"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._event_queue.full():
                with suppress(asyncio.QueueEmpty):
                    self._event_queue.get_nowait()
            with suppress(asyncio.QueueFull):
                self._event_queue.put_nowait(exc)

    async def _receive_json(self, timeout: float) -> dict[str, Any]:
        assert self._connection is not None
        try:
            raw = await asyncio.wait_for(self._connection.recv(), timeout)
        except TimeoutError as exc:
            raise LiveTranslateProviderError(
                "UPSTREAM_TIMEOUT", "等待 LiveTranslate 响应超时。"
            ) from exc
        if not isinstance(raw, str):
            raise LiveTranslateProviderError(
                "INVALID_UPSTREAM_FRAME", "LiveTranslate 返回了非 JSON Frame。"
            )
        return _parse_json_event(raw)

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if self._connection is None:
            raise LiveTranslateProviderError("CONNECTION_CLOSED", "上游连接未建立。")
        await self._connection.send(json.dumps(payload, ensure_ascii=False))

    def _raise_if_error(self, event: dict[str, Any]) -> None:
        if event.get("type") == "error":
            raise LiveTranslateProviderError.from_event(event)


def is_source_transcription_error(error: LiveTranslateProviderError) -> bool:
    value = " ".join(
        part
        for part in (error.code, error.message, error.param or "")
        if part
    ).lower()
    return "transcription" in value or "qwen3-asr" in value


def is_voice_clone_error(error: LiveTranslateProviderError) -> bool:
    value = " ".join(
        part
        for part in (error.code, error.message, error.param or "")
        if part
    ).lower()
    return "voice_clone" in value or "voice clone" in value


def _event_id() -> str:
    return f"event_{uuid.uuid4().hex}"


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_json_event(raw: str) -> dict[str, Any]:
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LiveTranslateProviderError(
            "INVALID_UPSTREAM_JSON", "LiveTranslate 返回了无效 JSON。"
        ) from exc
    if not isinstance(event, dict) or not isinstance(event.get("type"), str):
        raise LiveTranslateProviderError(
            "INVALID_UPSTREAM_EVENT", "LiveTranslate 返回的事件不完整。"
        )
    return event


def _drain_queue(queue: asyncio.Queue[Any]) -> None:
    while True:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            return
