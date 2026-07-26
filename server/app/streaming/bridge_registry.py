from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field

from ai_voice_interpreter.streaming.protocol import ErrorCode, ProtocolError

BRIDGE_ROLES = {"local_to_remote", "remote_to_local"}


def token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()[:16]


@dataclass(slots=True)
class BridgeRecord:
    bridge_id: str
    token_fingerprint: str
    created_at: float
    last_activity_at: float
    sessions: dict[str, str] = field(default_factory=dict)

    @property
    def state(self) -> str:
        return "active" if len(self.sessions) == 2 else "starting"


class BridgeRegistry:
    """Single-worker in-memory bridge membership without retaining bearer tokens."""

    def __init__(self, max_active_per_token: int = 1, ttl_seconds: int = 120) -> None:
        self.max_active_per_token = max_active_per_token
        self.ttl_seconds = ttl_seconds
        self._bridges: dict[str, BridgeRecord] = {}
        self._lock = asyncio.Lock()

    async def register(
        self,
        *,
        token: str,
        bridge_id: str,
        role: str,
        session_id: str,
    ) -> BridgeRecord:
        if role not in BRIDGE_ROLES:
            raise ProtocolError(ErrorCode.INVALID_SESSION_ROLE, "无效的 Bridge Session Role。")
        fingerprint = token_fingerprint(token)
        now = time.monotonic()
        async with self._lock:
            self._cleanup_locked(now)
            record = self._bridges.get(bridge_id)
            if record is None:
                active_for_token = sum(
                    item.token_fingerprint == fingerprint for item in self._bridges.values()
                )
                if active_for_token >= self.max_active_per_token:
                    raise ProtocolError(
                        ErrorCode.BRIDGE_LIMIT_REACHED,
                        "当前 Token 已有活动 Meeting Bridge。",
                    )
                record = BridgeRecord(bridge_id, fingerprint, now, now)
                self._bridges[bridge_id] = record
            elif record.token_fingerprint != fingerprint:
                raise ProtocolError(
                    ErrorCode.BRIDGE_LIMIT_REACHED,
                    "bridge_id 已属于其他客户端。",
                )
            if role in record.sessions:
                raise ProtocolError(
                    ErrorCode.BRIDGE_ROLE_CONFLICT,
                    f"bridge_id 已存在 {role} Session。",
                )
            if len(record.sessions) >= 2:
                raise ProtocolError(
                    ErrorCode.BRIDGE_LIMIT_REACHED,
                    "同一 bridge_id 最多两条方向 Session。",
                )
            record.sessions[role] = session_id
            record.last_activity_at = now
            return record

    async def touch(self, bridge_id: str) -> None:
        async with self._lock:
            record = self._bridges.get(bridge_id)
            if record is not None:
                record.last_activity_at = time.monotonic()

    async def release(self, bridge_id: str, role: str, session_id: str) -> None:
        async with self._lock:
            record = self._bridges.get(bridge_id)
            if record is None:
                return
            if record.sessions.get(role) == session_id:
                record.sessions.pop(role, None)
            record.last_activity_at = time.monotonic()
            if not record.sessions:
                self._bridges.pop(bridge_id, None)

    async def cleanup_expired(self) -> int:
        async with self._lock:
            return self._cleanup_locked(time.monotonic())

    def _cleanup_locked(self, now: float) -> int:
        expired = [
            bridge_id
            for bridge_id, record in self._bridges.items()
            if now - record.last_activity_at >= self.ttl_seconds
        ]
        for bridge_id in expired:
            self._bridges.pop(bridge_id, None)
        return len(expired)

    @property
    def active_bridges(self) -> int:
        return len(self._bridges)

    @property
    def active_directional_sessions(self) -> int:
        return sum(len(record.sessions) for record in self._bridges.values())

    def safe_snapshot(self) -> list[dict[str, object]]:
        return [
            {
                "bridge_id": record.bridge_id,
                "token_fingerprint": record.token_fingerprint,
                "roles": sorted(record.sessions),
                "state": record.state,
            }
            for record in self._bridges.values()
        ]
