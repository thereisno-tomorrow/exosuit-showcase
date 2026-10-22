"""Route only through durable GT bindings, never a caller-selected provider."""
from __future__ import annotations

from .errors import ReconciliationRequiredError, TransportError


class BoundTransport:
    def __init__(self, store, providers):
        self.store = store
        self.providers = dict(providers)

    def _provider(self, binding):
        provider = self.providers.get(binding)
        if provider is None:
            raise TransportError("SOURCE_UNAVAILABLE", "bound provider is unavailable", retryable=True)
        return provider

    def _operation(self, envelope):
        row = self.store.fetchone(
            """SELECT o.backend_binding, r.backend_binding AS request_binding
            FROM operations o JOIN requests r ON r.request_id=o.request_id
            WHERE o.operation_id=? AND o.request_id=? AND o.payload_sha256=? AND o.payload=?""",
            (envelope.operation_id, envelope.request_id, envelope.content_sha256, envelope.payload),
        )
        if row is None or row["backend_binding"] != row["request_binding"]:
            raise ReconciliationRequiredError("operation backend binding does not match durable request")
        return row["backend_binding"], self._provider(row["backend_binding"])

    def send(self, envelope):
        _, provider = self._operation(envelope)
        return provider.send(envelope)

    def reconcile(self, envelope):
        binding, provider = self._operation(envelope)
        result = provider.reconcile(envelope)
        if result is None and binding != "agentmail":
            raise ReconciliationRequiredError("unresolved provider acceptance; automatic resend is forbidden")
        return result

    def fetch_thread(self, thread_id, cursor=None):
        rows = self.store.fetchall(
            "SELECT DISTINCT backend_binding FROM requests WHERE thread_id=?", (thread_id,)
        )
        if len(rows) != 1:
            raise ReconciliationRequiredError("thread has no unambiguous durable backend binding")
        return self._provider(rows[0]["backend_binding"]).fetch_thread(thread_id, cursor)

    def fetch_message(self, message_id):
        row = self.store.fetchone(
            """SELECT r.backend_binding FROM message_links m
            JOIN requests r ON r.request_id=m.request_id WHERE m.provider_message_id=?""",
            (message_id,),
        )
        if row is None:
            raise ReconciliationRequiredError("message has no durable backend binding")
        return self._provider(row["backend_binding"]).fetch_message(message_id)
