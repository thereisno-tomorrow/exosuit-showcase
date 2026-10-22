from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from brain_core.contracts import ExportSpec, Grant, SearchSpec, VaultManifest
from brain_core.errors import BrainError, ErrorCode
from brain_core.policy import check_read_access, check_search_access, policy_sha256


NOW = datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc)


def manifest(root: Path, *, sensitivity: str = "private", vault_id: str = "vault_alpha") -> VaultManifest:
    return VaultManifest(
        schema="brain.vault/v1",
        id=vault_id,
        name="Alpha",
        purpose="Synthetic retrieval evidence",
        topics=("alpha", "testing"),
        sensitivity=sensitivity,
        sync_class="private-sync",
        default_branch="main",
        intent="intent.md",
        record_roots=("records",),
        dependencies=(),
        search=SearchSpec(("intent.md", "search-only.md")),
        exports=(ExportSpec("intent", ("intent.md",)),),
        root=str(root),
    )


def grant(
    *,
    vault: str = "vault_alpha",
    session_id: str = "session-a",
    paths: tuple[str, ...] = ("private.md",),
    issued_at: datetime = NOW - timedelta(minutes=5),
    expires_at: datetime = NOW + timedelta(minutes=5),
    operation: str = "read",
    grant_id: str = "grant_alpha",
) -> Grant:
    return Grant(
        schema="brain.grant/v1",
        id=grant_id,
        issuer="synthetic-user",
        authenticated_source="fixture://message",
        source_message_sha256="a" * 64,
        session_id=session_id,
        operation=operation,
        vault=vault,
        paths=paths,
        issued_at=issued_at,
        expires_at=expires_at,
    )


def test_search_and_read_distinguish_search_exports_and_exact_grants(tmp_path: Path) -> None:
    item = manifest(tmp_path)
    exact = grant()

    exported_search = check_search_access(item, "intent.md", (), NOW, "session-a", "local", "think")
    exported_read = check_read_access(item, "intent.md", (), NOW, "session-a", "local", "think")
    search_only = check_search_access(item, "search-only.md", (), NOW, "session-a", "local", "think")
    granted = check_read_access(item, "private.md", (exact,), NOW, "session-a", "local", "think")

    assert (exported_search.allowed, exported_search.access) == (True, "search")
    assert (search_only.allowed, search_only.access) == (True, "search")
    assert (exported_read.allowed, exported_read.access) == (True, "export")
    assert granted.allowed and granted.access == "session-grant"
    assert granted.grant_sha256 == exact.sha256


def test_search_access_applies_node_mode_and_grant_gates_to_undeclared_paths(tmp_path: Path) -> None:
    local_only = manifest(tmp_path, sensitivity="local-only")
    exact = grant()
    assert check_search_access(
        local_only, "private.md", (exact,), NOW, "session-a", "local", "think"
    ).access == "session-grant"

    wrong_node = check_search_access(
        local_only, "intent.md", (), NOW, "session-a", "cloud", "think"
    )
    wrong_mode = check_search_access(
        local_only, "intent.md", (), NOW, "session-a", "local", "plan"
    )
    missing = check_search_access(
        local_only, "private.md", (), NOW, "session-a", "local", "think"
    )
    assert wrong_node.code is ErrorCode.POLICY_EXCLUDED
    assert wrong_mode.code is ErrorCode.POLICY_EXCLUDED
    assert missing.code is ErrorCode.GRANT_MISSING


@pytest.mark.parametrize(
    ("grants", "path", "session_id", "code", "reason"),
    [
        ((), "private.md", "session-a", ErrorCode.GRANT_MISSING, "no session grant for vault"),
        ((grant(session_id="session-b"),), "private.md", "session-a", ErrorCode.GRANT_MISSING, "another session"),
        (
            (grant(expires_at=NOW),),
            "private.md",
            "session-a",
            ErrorCode.GRANT_EXPIRED,
            "not active",
        ),
        (
            (grant(issued_at=NOW + timedelta(seconds=1)),),
            "private.md",
            "session-a",
            ErrorCode.GRANT_EXPIRED,
            "not active",
        ),
        ((grant(paths=("other.md",)),), "private.md", "session-a", ErrorCode.GRANT_WRONG_PATH, "exact path"),
        ((grant(operation="write"),), "private.md", "session-a", ErrorCode.GRANT_WRONG_PATH, "exact path"),
    ],
)
def test_grant_failures_are_specific_and_do_not_disclose_a_path(
    tmp_path: Path,
    grants: tuple[Grant, ...],
    path: str,
    session_id: str,
    code: ErrorCode,
    reason: str,
) -> None:
    decision = check_read_access(manifest(tmp_path), path, grants, NOW, session_id, "local", "think")
    assert not decision.allowed
    assert decision.access == "excluded"
    assert decision.code is code
    assert reason in (decision.reason or "")
    assert decision.grant_sha256 is None


@pytest.mark.parametrize("mode", ["orient", "think", "execute"])
@pytest.mark.parametrize("node", ["local", "LOCAL", "laptop-nic", "Windows-11"])
def test_local_only_is_available_only_on_recognized_local_nodes(
    tmp_path: Path, mode: str, node: str
) -> None:
    decision = check_read_access(manifest(tmp_path, sensitivity="local-only"), "intent.md", (), NOW, "s", node, mode)
    expected = node == "local" or node.casefold().startswith(("laptop", "windows"))
    assert decision.allowed is expected
    if not expected:
        assert decision.code is ErrorCode.POLICY_EXCLUDED
        assert decision.reason == "vault is local-only on another node"


@pytest.mark.parametrize("mode", ["plan", "chat", "", "THINK"])
def test_non_retrieval_modes_are_excluded_before_export_or_grant_access(tmp_path: Path, mode: str) -> None:
    item = manifest(tmp_path)
    for path, grants in (("intent.md", ()), ("private.md", (grant(),))):
        decision = check_read_access(item, path, grants, NOW, "session-a", "local", mode)
        assert not decision.allowed
        assert decision.code is ErrorCode.POLICY_EXCLUDED
        assert decision.reason == "mode does not permit retrieval"


def test_policy_normalizes_repository_paths_and_refuses_traversal(tmp_path: Path) -> None:
    item = manifest(tmp_path)
    for path in ("folder\\../intent.md", "../intent.md"):
        with pytest.raises(BrainError) as caught:
            check_read_access(item, path, (), NOW, "s", "local", "think")
        assert caught.value.code is ErrorCode.PATH_TRAVERSAL


def test_policy_hash_is_order_independent_and_session_scoped(tmp_path: Path) -> None:
    alpha = manifest(tmp_path, vault_id="vault_alpha")
    beta = replace(manifest(tmp_path, vault_id="vault_beta"), name="Beta")
    one = grant(grant_id="grant_one")
    two = grant(paths=("two.md",), grant_id="grant_two")
    foreign = grant(session_id="other-session", grant_id="grant_foreign")

    first = policy_sha256((beta, alpha), (two, foreign, one), node="local", mode="think", session_id="session-a")
    second = policy_sha256((alpha, beta), (one, two), node="local", mode="think", session_id="session-a")
    assert first == second
    assert first != policy_sha256((alpha, beta), (one, two), node="cloud", mode="think", session_id="session-a")
    assert first != policy_sha256((alpha, beta), (one, two), node="local", mode="orient", session_id="session-a")
    assert first != policy_sha256((alpha, beta), (one, two), node="local", mode="think", session_id="session-b")
