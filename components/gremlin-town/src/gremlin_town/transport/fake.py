"""Deterministic provider transport used by integration and failure-matrix tests."""

from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime
from enum import StrEnum
from typing import Callable

from gremlin_town.contracts import (
    ContentSource,
    OutboundEnvelope,
    Page,
    ProviderMessage,
    ProviderReference,
)

from .errors import ProviderTransportError, ThreadScopeConflictError


class FakeOutcome(StrEnum):
    SUCCESS = "success"
    TIMEOUT_BEFORE_ACCEPTANCE = "timeout_before_acceptance"
    TIMEOUT_AFTER_ACCEPTANCE = "timeout_after_acceptance"
    OUTAGE = "outage"


class FakeTransport:
    """A deterministic in-memory provider with explicit, injectable failures.

    The fake stores bytes exactly as received.  It keys acceptance by the
    provider retry key and rejects a changed payload under the same key.
    """

    def __init__(
        self,
        *,
        outcome: FakeOutcome = FakeOutcome.SUCCESS,
        page_size: int = 2,
        reorder: bool = False,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        self.outcome = outcome
        self.page_size = page_size
        self.reorder = reorder
        self._clock = clock or (lambda: datetime.now(UTC))
        self.sent_payloads: OrderedDict[str, bytes] = OrderedDict()
        self._references: dict[str, ProviderReference] = {}
        self._messages: OrderedDict[str, ProviderMessage] = OrderedDict()
        self._message_order: list[str] = []
        self.send_attempts = 0

    @property
    def messages(self) -> tuple[ProviderMessage, ...]:
        return tuple(self._messages.values())

    def send(self, envelope: OutboundEnvelope) -> ProviderReference:
        self.send_attempts += 1
        prior = self._references.get(envelope.provider_retry_key)
        prior_payload = self.sent_payloads.get(envelope.provider_retry_key)
        if prior is not None:
            if prior_payload != envelope.payload:
                raise ProviderTransportError("OPERATION_INPUT_CONFLICT", "retry key reused with different bytes")
            return prior
        if self.outcome is FakeOutcome.OUTAGE:
            raise ProviderTransportError("PROVIDER_UNAVAILABLE", "fake provider outage", retryable=True)
        if self.outcome is FakeOutcome.TIMEOUT_BEFORE_ACCEPTANCE:
            raise ProviderTransportError(
                "TIMEOUT_BEFORE_ACCEPTANCE", "fake timed out before acceptance", retryable=True
            )

        reference = self._accept(envelope)
        if self.outcome is FakeOutcome.TIMEOUT_AFTER_ACCEPTANCE:
            raise ProviderTransportError(
                "TIMEOUT_AFTER_ACCEPTANCE",
                "fake timed out after provider acceptance",
                retryable=True,
                accepted=True,
            )
        return reference

    def _accept(self, envelope: OutboundEnvelope) -> ProviderReference:
        now = self._clock()
        message_id = f"fake-msg-{len(self._references) + 1:04d}"
        thread_id = envelope.request_id
        reference = ProviderReference(
            message_id=message_id,
            thread_id=thread_id,
            accepted_at=now,
            parent_message_id=envelope.parent_message_id,
        )
        self.sent_payloads[envelope.provider_retry_key] = bytes(envelope.payload)
        self._references[envelope.provider_retry_key] = reference
        self._messages[message_id] = ProviderMessage(
            message_id=message_id,
            thread_id=thread_id,
            parent_message_id=envelope.parent_message_id,
            received_at=now,
            body=bytes(envelope.payload),
            body_available=True,
            # The fake's accepted message represents provider content. The
            # local immutable outbox copy is owned by the hub, not the transport.
            content_source=ContentSource.PROVIDER,
            trust_label="provider_acceptance",
            linked_operation_id=envelope.operation_id,
            exact_link_verified=True,
        )
        self._message_order.append(message_id)
        return reference

    def inject_message(self, message: ProviderMessage) -> None:
        self._messages[message.message_id] = message
        if message.message_id not in self._message_order:
            self._message_order.append(message.message_id)

    def fetch_message(self, message_id: str) -> ProviderMessage:
        try:
            return self._messages[message_id]
        except KeyError as exc:
            raise ProviderTransportError("NOT_FOUND", "provider message was not found", status=404) from exc

    def fetch_thread(self, thread_id: str, cursor: str | None = None) -> Page:
        ids = [message_id for message_id in self._message_order if self._messages[message_id].thread_id == thread_id]
        if self.reorder:
            ids.reverse()
        start = int(cursor or "0")
        selected = ids[start : start + self.page_size]
        items: list[dict[str, object]] = []
        for message_id in selected:
            message = self._messages[message_id]
            if message.thread_id != thread_id:
                raise ThreadScopeConflictError()
            items.append({"message": message})
        next_cursor = str(start + self.page_size) if start + self.page_size < len(ids) else None
        return Page(items=items, next_cursor=next_cursor, as_of=self._clock())

    def fetch_thread_complete(self, thread_id: str) -> tuple[ProviderMessage, ...]:
        cursor: str | None = None
        found: dict[str, ProviderMessage] = {}
        while True:
            page = self.fetch_thread(thread_id, cursor)
            for item in page.items:
                message = item["message"]
                if not isinstance(message, ProviderMessage) or message.thread_id != thread_id:
                    raise ThreadScopeConflictError()
                found[message.message_id] = message
            if page.next_cursor is None:
                return tuple(found.values())
            cursor = page.next_cursor

    def reconcile(self, envelope: OutboundEnvelope) -> ProviderReference | None:
        prior = self._references.get(envelope.provider_retry_key)
        if prior is not None and self.sent_payloads.get(envelope.provider_retry_key) != envelope.payload:
            raise ProviderTransportError("OPERATION_INPUT_CONFLICT", "retry key reused with different bytes")
        return prior
