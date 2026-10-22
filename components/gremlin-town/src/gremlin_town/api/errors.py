"""Consistent, non-leaky HTTP error mapping for service results."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from fastapi import Request

from gremlin_town.contracts import ErrorCode, Receipt


class ClientHttpError(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message
        super().__init__(message)


_T = TypeVar("_T")


def _code_value(code: Any) -> str:
    return getattr(code, "value", str(code))


def status_for_code(code: ErrorCode | str, *, resource: bool = False) -> int:
    code_value = _code_value(code)
    if code_value == ErrorCode.ACCESS_DENIED.value:
        # Resource routes intentionally use the same response for a missing
        # resource and an inaccessible one.
        return 404 if resource else 403
    if code_value in {
        ErrorCode.REVISION_CONFLICT.value,
        ErrorCode.OPERATION_INPUT_CONFLICT.value,
        ErrorCode.INVALID_TRANSITION.value,
        ErrorCode.THREAD_SCOPE_CONFLICT.value,
    }:
        return 409
    if code_value == ErrorCode.RESULT_PENDING.value:
        return 409
    if code_value == ErrorCode.SOURCE_UNAVAILABLE.value:
        return 503
    if code_value in {
        ErrorCode.UNSUPPORTED_SCHEMA.value,
        ErrorCode.PACKET_TOO_LARGE.value,
        ErrorCode.RECIPIENT_AMBIGUOUS.value,
    }:
        return 422
    if code_value == ErrorCode.RECOVERY_HOLD.value:
        return 423
    if code_value == ErrorCode.RECOVERY_REQUIRED.value:
        return 409
    return 400


def raise_for_receipt(receipt: Receipt, *, resource: bool = False) -> Receipt:
    if receipt.error is None:
        return receipt
    code = _code_value(receipt.error.code)
    if code == ErrorCode.ACCESS_DENIED.value:
        raise ClientHttpError(
            status_for_code(code, resource=resource),
            code,
            "resource unavailable or access denied",
        )
    raise ClientHttpError(
        status_for_code(code, resource=resource),
        code,
        receipt.error.message,
    )


def raise_for_service_exception(exc: Exception, *, resource: bool = False) -> None:
    raw_code = getattr(exc, "code", None)
    if isinstance(exc, PermissionError):
        code = ErrorCode.ACCESS_DENIED.value
    elif raw_code is not None:
        code = _code_value(raw_code)
    else:
        raise ClientHttpError(500, "INTERNAL_ERROR", "internal service error") from exc
    message = "resource unavailable or access denied" if code == ErrorCode.ACCESS_DENIED.value else str(exc)
    raise ClientHttpError(status_for_code(code, resource=resource), code, message) from exc


async def call_service(
    request: Request,
    method_name: str,
    *args: Any,
    resource: bool = False,
) -> Any:
    """Call exactly one service operation and normalize service failures."""

    service = request.app.state.service
    method: Callable[..., Any] = getattr(service, method_name)
    try:
        value = method(*args)
        if inspect.isawaitable(value):
            value = await value
    except ClientHttpError:
        raise
    except Exception as exc:
        raise_for_service_exception(exc, resource=resource)
    if isinstance(value, Receipt):
        return raise_for_receipt(value, resource=resource)
    return value
