"""Provider transport implementations and packet framing utilities."""

from .agentmail import AgentMailConfig, AgentMailTransport
from .github import GitHubConfig, GitHubTransport
from .routing import BoundTransport
from .errors import (
    ProviderTransportError,
    ReconciliationRequiredError,
    ThreadScopeConflictError,
    TransportError,
)
from .fake import FakeOutcome, FakeTransport
from .framing import canonical_wire_bytes, frame_wire_message, parse_wire_message

__all__ = [
    "BoundTransport",
    "GitHubConfig",
    "GitHubTransport",
    "AgentMailConfig",
    "AgentMailTransport",
    "FakeOutcome",
    "FakeTransport",
    "ProviderTransportError",
    "ReconciliationRequiredError",
    "ThreadScopeConflictError",
    "TransportError",
    "frame_wire_message",
    "canonical_wire_bytes",
    "parse_wire_message",
]
