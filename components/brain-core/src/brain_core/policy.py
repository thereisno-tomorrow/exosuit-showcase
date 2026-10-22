"""Pure policy decisions for search ingestion and source disclosure."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from brain_core.contracts import Grant, VaultManifest, stable_hash
from brain_core.errors import ErrorCode
from brain_core.paths import normalize_relative

AccessKind = Literal["export", "search", "session-grant", "excluded"]


@dataclass(frozen=True, slots=True)
class AccessDecision:
    allowed: bool
    access: AccessKind
    code: ErrorCode | None = None
    reason: str | None = None
    grant_sha256: str | None = None


def _node_allows(manifest: VaultManifest, node: str) -> bool:
    return not (
        manifest.sensitivity == "local-only"
        and not (node == "local" or node.casefold().startswith(("laptop", "windows")))
    )


def _mode_allows(mode: str) -> bool:
    return mode in {"orient", "think", "execute"}


def _grant_decision(
    manifest: VaultManifest,
    path: str,
    grants: tuple[Grant, ...],
    now: datetime,
    session_id: str,
) -> AccessDecision:
    vault_grants = tuple(grant for grant in grants if grant.vault == manifest.id)
    if not vault_grants:
        return AccessDecision(False, "excluded", ErrorCode.GRANT_MISSING, "no session grant for vault")
    session_grants = tuple(grant for grant in vault_grants if grant.session_id == session_id)
    if not session_grants:
        return AccessDecision(False, "excluded", ErrorCode.GRANT_MISSING, "grant belongs to another session")
    active = tuple(grant for grant in session_grants if grant.issued_at <= now < grant.expires_at)
    if not active:
        return AccessDecision(False, "excluded", ErrorCode.GRANT_EXPIRED, "grant is not active")
    exact = next((grant for grant in active if grant.operation == "read" and path in grant.paths), None)
    if exact is None:
        return AccessDecision(False, "excluded", ErrorCode.GRANT_WRONG_PATH, "grant does not name this exact path")
    return AccessDecision(True, "session-grant", grant_sha256=exact.sha256)


def check_search_access(
    manifest: VaultManifest,
    path: str,
    grants: tuple[Grant, ...],
    now: datetime,
    session_id: str,
    node: str,
    mode: str,
) -> AccessDecision:
    relative = normalize_relative(path)
    if not _node_allows(manifest, node):
        return AccessDecision(False, "excluded", ErrorCode.POLICY_EXCLUDED, "vault is local-only on another node")
    if not _mode_allows(mode):
        return AccessDecision(False, "excluded", ErrorCode.POLICY_EXCLUDED, "mode does not permit retrieval")
    if relative in manifest.search.paths or relative in manifest.export_paths:
        return AccessDecision(True, "search")
    return _grant_decision(manifest, relative, grants, now, session_id)


def check_read_access(
    manifest: VaultManifest,
    path: str,
    grants: tuple[Grant, ...],
    now: datetime,
    session_id: str,
    node: str,
    mode: str,
) -> AccessDecision:
    relative = normalize_relative(path)
    if not _node_allows(manifest, node):
        return AccessDecision(False, "excluded", ErrorCode.POLICY_EXCLUDED, "vault is local-only on another node")
    if not _mode_allows(mode):
        return AccessDecision(False, "excluded", ErrorCode.POLICY_EXCLUDED, "mode does not permit retrieval")
    if relative in manifest.export_paths:
        return AccessDecision(True, "export")
    return _grant_decision(manifest, relative, grants, now, session_id)


def policy_sha256(
    manifests: tuple[VaultManifest, ...],
    grants: tuple[Grant, ...],
    *,
    node: str,
    mode: str,
    session_id: str,
) -> str:
    payload = {
        "manifests": [manifest.as_dict() for manifest in sorted(manifests, key=lambda item: item.id)],
        "grant_sha256": sorted(grant.sha256 for grant in grants if grant.session_id == session_id),
        "node": node,
        "mode": mode,
        "session_id": session_id,
    }
    return stable_hash(payload)
