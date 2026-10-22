"""Per-call attribution from verified host identity, never operation arguments."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
import hashlib
import json

@dataclass(frozen=True, slots=True)
class WriteAttribution:
    authenticated_subject: str
    executing_agent: str
    recipient_hub_id: str | None = None

_CURRENT: ContextVar[WriteAttribution | None] = ContextVar("brain_write_attribution", default=None)

def current_write_attribution():
    value = _CURRENT.get()
    return asdict(value) if value else None

@contextmanager
def bind_write_attribution(subject: str):
    # Distinct credentials identify the executing agent; standalone BC has no GT recipient.
    token = _CURRENT.set(WriteAttribution(subject, subject))
    try:
        yield
    finally:
        _CURRENT.reset(token)

def attribute_evidence(evidence):
    attribution = current_write_attribution()
    if attribution is None:
        return evidence
    payload = json.dumps(attribution, sort_keys=True, separators=(",", ":"))
    envelope = json.dumps({"source_sha256": evidence.content_sha256, "attribution": attribution}, sort_keys=True, separators=(",", ":"))
    return replace(evidence, actor=attribution["authenticated_subject"],
        excerpt=evidence.excerpt + " " + payload,
        content_sha256=hashlib.sha256(envelope.encode()).hexdigest())
