from __future__ import annotations

import asyncio
import time

import pytest

from ai_voice_interpreter.streaming.protocol import ErrorCode, ProtocolError
from server.app.streaming.bridge_registry import BridgeRegistry, token_fingerprint


def run(coroutine):  # type: ignore[no-untyped-def]
    return asyncio.run(coroutine)


def test_two_directions_register_and_release_without_retaining_token() -> None:
    async def scenario() -> None:
        registry = BridgeRegistry()
        token = "super-secret-gateway-token"
        await registry.register(
            token=token,
            bridge_id="bridge-a",
            role="local_to_remote",
            session_id="session-a",
        )
        await registry.register(
            token=token,
            bridge_id="bridge-a",
            role="remote_to_local",
            session_id="session-b",
        )
        assert registry.active_bridges == 1
        assert registry.active_directional_sessions == 2
        snapshot = registry.safe_snapshot()
        assert snapshot[0]["state"] == "active"
        assert token not in repr(snapshot)
        assert snapshot[0]["token_fingerprint"] == token_fingerprint(token)

        await registry.release("bridge-a", "local_to_remote", "session-a")
        assert registry.active_bridges == 1
        assert registry.active_directional_sessions == 1
        await registry.release("bridge-a", "remote_to_local", "session-b")
        assert registry.active_bridges == 0
        assert registry.active_directional_sessions == 0

    run(scenario())


def test_duplicate_role_and_second_bridge_are_rejected() -> None:
    async def scenario() -> None:
        registry = BridgeRegistry(max_active_per_token=1)
        await registry.register(
            token="token",
            bridge_id="bridge-a",
            role="local_to_remote",
            session_id="one",
        )
        with pytest.raises(ProtocolError) as duplicate:
            await registry.register(
                token="token",
                bridge_id="bridge-a",
                role="local_to_remote",
                session_id="two",
            )
        assert duplicate.value.code == ErrorCode.BRIDGE_ROLE_CONFLICT
        with pytest.raises(ProtocolError) as second_bridge:
            await registry.register(
                token="token",
                bridge_id="bridge-b",
                role="remote_to_local",
                session_id="three",
            )
        assert second_bridge.value.code == ErrorCode.BRIDGE_LIMIT_REACHED

    run(scenario())


def test_third_session_and_other_token_cannot_join_existing_bridge() -> None:
    async def scenario() -> None:
        registry = BridgeRegistry()
        for role, session_id in (
            ("local_to_remote", "one"),
            ("remote_to_local", "two"),
        ):
            await registry.register(
                token="token",
                bridge_id="bridge-a",
                role=role,
                session_id=session_id,
            )
        with pytest.raises(ProtocolError):
            await registry.register(
                token="token",
                bridge_id="bridge-a",
                role="local_to_remote",
                session_id="three",
            )
        with pytest.raises(ProtocolError) as other_token:
            await registry.register(
                token="different",
                bridge_id="bridge-a",
                role="remote_to_local",
                session_id="four",
            )
        assert other_token.value.code == ErrorCode.BRIDGE_LIMIT_REACHED

    run(scenario())


def test_expired_bridge_is_cleaned() -> None:
    async def scenario() -> None:
        registry = BridgeRegistry(ttl_seconds=1)
        record = await registry.register(
            token="token",
            bridge_id="bridge-a",
            role="local_to_remote",
            session_id="one",
        )
        record.last_activity_at = time.monotonic() - 2
        assert await registry.cleanup_expired() == 1
        assert registry.active_bridges == 0

    run(scenario())


def test_invalid_role_is_rejected() -> None:
    async def scenario() -> None:
        registry = BridgeRegistry()
        with pytest.raises(ProtocolError) as error:
            await registry.register(
                token="token",
                bridge_id="bridge-a",
                role="third_direction",
                session_id="one",
            )
        assert error.value.code == ErrorCode.INVALID_SESSION_ROLE

    run(scenario())
