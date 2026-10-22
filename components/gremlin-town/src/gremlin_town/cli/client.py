"""Small authenticated HTTP client used by every CLI operation."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx


class CliError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "CLI_ERROR",
        status_code: int = 1,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(message)


class JsonApiClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout: float = 10.0,
        transport: httpx.BaseTransport | httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.transport = transport

    def request(
        self,
        method: str,
        path: str,
        *,
        query: Mapping[str, Any] | None = None,
        payload: Mapping[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Any:
        headers = {"Authorization": f"Bearer {self.token}", "Accept": "application/json"}
        try:
            with httpx.Client(timeout=self.timeout if timeout is None else timeout, transport=self.transport) as client:
                response = client.request(
                    method,
                    f"{self.base_url}{path}",
                    headers=headers,
                    params={key: value for key, value in (query or {}).items() if value is not None},
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise CliError(f"request failed: {exc}", code="NETWORK_ERROR", status_code=2) from exc
        try:
            data = response.json()
        except ValueError as exc:
            raise CliError("server returned non-JSON content", code="INVALID_RESPONSE", status_code=2) from exc
        if response.is_error:
            error = data.get("error", {}) if isinstance(data, dict) else {}
            raise CliError(
                str(error.get("message", "request rejected")),
                code=str(error.get("code", "HTTP_ERROR")),
                status_code=2,
                retryable=bool(error.get("retryable")),
            )
        return data
