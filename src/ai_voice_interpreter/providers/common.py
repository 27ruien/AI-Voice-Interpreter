from __future__ import annotations

from typing import Any

from ..config import AppConfig


def configure_dashscope(config: AppConfig) -> Any:
    """Apply SDK globals immediately before a provider call, never at import time."""
    import dashscope

    dashscope.api_key = config.dashscope_api_key
    dashscope.base_http_api_url = config.http_base_url
    dashscope.base_websocket_api_url = config.websocket_base_url
    return dashscope


def request_id_from(value: Any) -> str | None:
    for attribute in ("request_id", "get_request_id"):
        candidate = getattr(value, attribute, None)
        try:
            result = candidate() if callable(candidate) else candidate
        except Exception:  # SDK diagnostics must not mask the provider result.
            continue
        if result:
            return str(result)
    return None


def friendly_service_message(message: object, status_code: object = None) -> str:
    raw = _safe_provider_text(message)
    lowered = raw.lower()
    status = _safe_provider_text(
        status_code or _safe_provider_attribute(message, "status_code"), fallback=""
    )
    if any(
        token in lowered
        for token in ("accessdenied", "access denied", "unpurchased", "eligible")
    ):
        return "DashScope 模型访问被拒绝，请在当前地域和 Workspace 开通对应模型服务。"
    authentication_tokens = ("unauthorized", "api key", "authentication")
    if status in {"401", "403"} or any(token in lowered for token in authentication_tokens):
        return "DashScope 鉴权失败，请检查 API Key、地域和 Workspace 配置。"
    if any(token in lowered for token in ("quota", "balance", "arrearage", "insufficient")):
        return "DashScope 额度不足或账户欠费，请检查百炼控制台。"
    if any(token in lowered for token in ("model", "not found", "unsupported")):
        return "模型或音色不可用，请检查模型名、地域以及音色与模型是否匹配。"
    if any(token in lowered for token in ("timeout", "timed out")):
        return "请求超时，请检查网络后重试。"
    if any(token in lowered for token in ("connection", "network", "dns")):
        return "无法连接 DashScope，请检查网络和服务地址。"
    return f"DashScope 请求失败：{raw}"


def _safe_provider_text(value: object, *, fallback: str = "未知服务错误") -> str:
    if value is None:
        return fallback
    candidate = _safe_provider_attribute(value, "message")
    if candidate is not None and candidate is not value:
        return _safe_provider_text(candidate, fallback=fallback)
    try:
        return str(value)
    except Exception:
        return fallback


def _safe_provider_attribute(value: object, name: str) -> object | None:
    try:
        return getattr(value, name, None)
    except Exception:
        return None
