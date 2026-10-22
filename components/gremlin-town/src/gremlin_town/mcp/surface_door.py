"""Host-only admission for an authenticated owner tap; never an MCP argument.

The Discord interaction or dashboard session must authenticate its owner before
calling this boundary. The ordinary owner policy, move checks and replay checks
still run. Merely naming a system caller cannot provide tap authority.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
import hashlib
import json

REVIEW_CARD_CALLER = "system:review-card"
DASHBOARD_CALLER = "system:dashboard"
SURFACE_CALLERS = frozenset({REVIEW_CARD_CALLER, DASHBOARD_CALLER})


def configured_code_callers(table):
    table = table or {}
    callers = set()
    for key, caller in (("review_card", REVIEW_CARD_CALLER), ("dashboard", DASHBOARD_CALLER)):
        value = table.get(key, False)
        if not isinstance(value, bool):
            raise ValueError(f"work.{key} must be a boolean")
        if value:
            callers.add(caller)
    return frozenset(callers)


def _request_hash(request):
    return hashlib.sha256(json.dumps(asdict(request), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class _OwnerTap:
    caller: str
    owner_id: str
    request_hash: str


_TAP: ContextVar[_OwnerTap | None] = ContextVar("authenticated_owner_tap", default=None)


@contextmanager
def bind_owner_tap(caller, owner_id, request):
    """Trusted auth adapter only: bind the verified owner to this exact request."""
    if caller not in SURFACE_CALLERS or not isinstance(owner_id, str) or not owner_id:
        raise ValueError("An authenticated surface owner is required")
    token = _TAP.set(_OwnerTap(caller, owner_id, _request_hash(request)))
    try:
        yield
    finally:
        _TAP.reset(token)


def owner_tap_matches(caller, owner_id, request):
    tap = _TAP.get()
    return (tap is not None and tap.caller == caller and tap.owner_id == owner_id
            and tap.request_hash == _request_hash(request)
            and request.said_by == "owner" and request.came_by == "direct")
