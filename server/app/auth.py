from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import Header

from .errors import GatewayHTTPError


class BearerTokenAuth:
    def __init__(self, expected_token: str) -> None:
        self.expected_token = expected_token

    def __call__(
        self,
        authorization: Annotated[str | None, Header()] = None,
    ) -> None:
        prefix = "Bearer "
        supplied = (
            authorization[len(prefix) :]
            if authorization and authorization.startswith(prefix)
            else ""
        )
        if not supplied or not secrets.compare_digest(supplied, self.expected_token):
            raise GatewayHTTPError(401, "unauthorized", "Bearer Token 缺失或无效。")
