from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import brain_core.retrieval as retrieval
from brain_core.contracts import (
    Budget,
    ContextRequest,
    ExportSpec,
    Grant,
    SearchSpec,
    VaultManifest,
)
from brain_core.errors import BrainError, ErrorCode
from brain_core.paths import FileSnapshot
from brain_core.retrieval import (
    build_context,
    derive_facets,
    excerpt_for_facets,
    plan_context,
    rank_vaults,
    requested_facets,
    token_count,
    tokenize,
)


NOW = datetime(2026, 8, 29, 1, 0, tzinfo=timezone.utc)


def manifest(
    root: Path,
    vault_id: str,
    *,
    name: str | None = None,
    purpose: str = "Synthetic evidence",
    topics: tuple[str, ...] = (),
    intent: str | None = "intent.md",
    search: tuple[str, ...] = ("intent.md",),
    exports: tuple[str, ...] = ("intent.md",),
    dependencies: tuple[str, ...] = (),
    sensitivity: str = "private",
) -> VaultManifest:
    return VaultManifest(
        schema="brain.vault/v1",
        id=vault_id,
        name=name or vault_id,
        purpose=purpose,
        topics=topics,
        sensitivity=sensitivity,
        sync_class="private-sync",
        default_branch="main",
        intent=intent,
        record_roots=(),
        dependencies=dependencies,
        search=SearchSpec(search),
        exports=(ExportSpec("export", exports),) if exports else (),
        root=str(root),
    )


def grant(vault: str, paths: tuple[str, ...], *, session_id: str = "session-a") -> Grant:
    return Grant(
        schema="brain.grant/v1",
        id=f"grant_{vault.removeprefix('vault_')}_{len(paths)}",
        issuer="synthetic-user",
        authenticated_source="fixture://message",
        source_message_sha256="a" * 64,
        session_id=session_id,
        operation="read",
        vault=vault,
        paths=paths,
        issued_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(minutes=5),
    )


def request(
    *,
    intent: str = "alpha evidence",
    facets: tuple[str, ...] = ("alpha",),
    named: tuple[str, ...] = (),
    grants: tuple[Grant, ...] = (),
    budget: Budget = Budget(source_tokens=12_000, vaults=4, excerpts=12),
    expansion: str | None = None,
    node: str = "local",
    mode: str = "think",
) -> ContextRequest:
    return ContextRequest(
        request_id="request-1",
        session_id="session-a",
        intent=intent,
        named_vaults=named,
        requested_facets=facets,
        grants=grants,
        budget=budget,
        expansion_facet=expansion,
        node=node,
        mode=mode,
    )


def write(root: Path, relative: str, text: str | bytes) -> dict[str, object]:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    raw = text if isinstance(text, bytes) else text.encode("utf-8")
    target.write_bytes(raw)
    return {
        "content_sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }


def match(vault: VaultManifest, path: str, *, rank: float = 1.0, **metadata: object) -> dict[str, object]:
    return {
        "vault_id": vault.id,
        "path": path,
        "root": vault.root,
        "content_sha256": None,
        "size": 0,
        "search_only": False,
        "exported": path in vault.export_paths,
        "rank": rank,
        **metadata,
    }


def install_matches(monkeypatch: pytest.MonkeyPatch, matches: tuple[object, ...]) -> None:
    monkeypatch.setattr(retrieval, "_query", lambda *_args, **_kwargs: matches)


def test_lexical_facets_are_deterministic_deduplicated_and_bounded() -> None:
    assert tokenize("Alpha-beta, café!") == ("Alpha", "-", "beta", ",", "café", "!")
    assert token_count("Alpha-beta, café!") == 6
    assert derive_facets("What is THE Alpha alpha beta? Café and gamma") == (
        "alpha",
        "beta",
        "café",
        "gamma",
    )
    assert derive_facets("one two three four", limit=2) == ("one", "two")


def test_requested_facets_adds_at_most_one_normalized_expansion() -> None:
    assert requested_facets(request(facets=(), intent="alpha and beta")) == ("alpha", "beta")
    assert requested_facets(request(facets=("alpha",), expansion="  BETA ")) == ("alpha", "beta")
    assert requested_facets(request(facets=("alpha",), expansion=" ALPHA ")) == ("alpha",)
    assert requested_facets(request(facets=("alpha",), expansion="   ")) == ("alpha",)


def test_rank_vaults_combines_named_dependency_topic_and_match_scores_with_stable_ties(tmp_path: Path) -> None:
    alpha = manifest(
        tmp_path / "a",
        "vault_alpha",
        name="Alpha Strategy",
        purpose="beta evidence",
        topics=("gamma",),
        dependencies=("vault_beta",),
    )
    beta = manifest(tmp_path / "b", "vault_beta", name="Neutral")
    charlie = manifest(tmp_path / "c", "vault_charlie", topics=("alpha", "beta"))
    delta = manifest(tmp_path / "d", "vault_delta", topics=("alpha", "beta"))
    matches = (
        match(charlie, "z.md", rank=50),
        match(charlie, "a.md", rank=5),
        match(delta, "a.md", rank=20),
    )
    ranked = rank_vaults(
        (delta, charlie, beta, alpha),
        matches,
        request(named=("vault_alpha",), facets=("alpha", "beta")),
    )
    assert [(item.manifest.id, item.score, item.named) for item in ranked] == [
        ("vault_alpha", 130.0, True),
        ("vault_charlie", 50.0, False),
        ("vault_delta", 50.0, False),
        ("vault_beta", 30.0, False),
    ]
    assert [item["path"] for item in ranked[1].matches] == ["z.md", "a.md"]


def test_rank_vaults_supports_object_matches_and_omits_zero_signal_vaults(tmp_path: Path) -> None:
    alpha = manifest(tmp_path / "a", "vault_alpha")
    beta = manifest(tmp_path / "b", "vault_beta")

    class ObjectMatch:
        vault_id = "vault_beta"
        path = "evidence.md"
        rank = 3.5

    ranked = rank_vaults((alpha, beta), (ObjectMatch(),), request(facets=("unrelated",)))
    assert [(item.manifest.id, item.score) for item in ranked] == [("vault_beta", 3.5)]


def test_rank_vaults_rejects_missing_named_vaults_and_named_budget_overflow(tmp_path: Path) -> None:
    alpha = manifest(tmp_path, "vault_alpha")
    with pytest.raises(BrainError) as missing:
        rank_vaults((alpha,), (), request(named=("vault_missing",)))
    assert missing.value.code is ErrorCode.NOT_FOUND
    assert missing.value.details == {"missing_vaults": ["vault_missing"]}

    with pytest.raises(BrainError) as overflow:
        rank_vaults(
            (alpha,),
            (),
            request(named=("vault_alpha",), budget=Budget(source_tokens=10, vaults=0, excerpts=1)),
        )
    assert overflow.value.code is ErrorCode.INVALID_ARGUMENT


def test_excerpt_prefers_maximum_facet_coverage_then_source_order_and_preserves_locator() -> None:
    text = "# Intro\nalpha only\n\n## Strong\nalpha beta evidence\n\nlate gamma"
    excerpt, locator, covered = excerpt_for_facets(text, ("alpha", "beta"), max_tokens=20)
    assert excerpt == "## Strong\nalpha beta evidence"
    assert locator == "heading:Strong"
    assert covered == {"alpha", "beta"}

    empty, empty_locator, empty_coverage = excerpt_for_facets("\n\n", ("alpha",), max_tokens=5)
    assert (empty, empty_locator, empty_coverage) == ("", "line:1", frozenset())


def test_excerpt_honors_token_limit_skips_nonfitting_later_blocks_and_truncates_first() -> None:
    text = "alpha one two three\n\nbeta four five\n\nalpha beta"
    excerpt, _, covered = excerpt_for_facets(text, ("alpha", "beta"), max_tokens=3)
    assert token_count(excerpt) == 2
    assert excerpt == "alpha beta"
    assert covered == {"alpha", "beta"}

    truncated, _, truncated_coverage = excerpt_for_facets("alpha one two three", ("alpha",), max_tokens=2)
    assert truncated == "alpha one"
    assert token_count(truncated) == 2
    assert truncated_coverage == {"alpha"}

    reordered, _, reordered_coverage = excerpt_for_facets(
        "alpha early\n\nalpha beta strongest\n\nlong unrelated block that cannot fit",
        ("alpha", "beta", "gamma"),
        max_tokens=6,
    )
    assert reordered == "alpha early\n\nalpha beta strongest"
    assert reordered_coverage == {"alpha", "beta"}


def test_query_adapter_forwards_the_fixed_index_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import brain_core.index as index

    calls: list[tuple[object, object, bool, int]] = []

    def fake(database: object, intent: object, *, allow_stale: bool, limit: int) -> tuple[str, ...]:
        calls.append((database, intent, allow_stale, limit))
        return ("match",)

    monkeypatch.setattr(index, "query_index", fake)
    assert retrieval._query("index.db", "alpha", True) == ("match",)
    assert calls == [("index.db", "alpha", True, 200)]


def test_plan_context_is_body_free_deterministic_and_supports_degraded_grants(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "alpha"
    item = manifest(root, "vault_alpha", topics=("alpha",), exports=())
    exact = grant(item.id, ("private.md",))
    install_matches(monkeypatch, (match(item, "search-only.md", rank=2),))
    planned = plan_context(
        "unused.db",
        (item,),
        request(named=(item.id,), grants=(exact,), expansion="beta", budget=Budget(99, 1, 2)),
        allow_stale=True,
    )
    assert planned["requested_facets"] == ["alpha", "beta"]
    assert planned["planned_vaults"] == [item.id]
    assert planned["budget"] == {"source_tokens": 99, "vaults": 1, "excerpts": 2}
    assert planned["grant_sha256"] == [exact.sha256]
    assert planned["note"].startswith("Final cutoff")
    assert "sources" not in planned
    assert "private.md" not in str(planned)

    def missing(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise BrainError(ErrorCode.INDEX_MISSING, "missing")

    monkeypatch.setattr(retrieval, "_query", missing)
    degraded = plan_context("missing.db", (item,), request(grants=(exact,)))
    assert degraded["planned_vaults"] == [item.id]

    with pytest.raises(BrainError) as caught:
        plan_context("missing.db", (item,), request())
    assert caught.value.code is ErrorCode.INDEX_MISSING

    def unrelated(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise BrainError(ErrorCode.NOT_FOUND, "other")

    monkeypatch.setattr(retrieval, "_query", unrelated)
    with pytest.raises(BrainError) as other:
        plan_context("missing.db", (item,), request(grants=(exact,)))
    assert other.value.code is ErrorCode.NOT_FOUND


def test_build_context_reads_exports_and_exact_grants_with_provenance(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "alpha"
    exported = write(root, "intent.md", "# Current\nalpha orientation")
    private = write(root, "records/private.md", "# Evidence\nalpha private proof")
    item = manifest(
        root,
        "vault_alpha",
        intent="intent.md",
        search=("intent.md",),
        exports=("intent.md",),
        topics=("alpha",),
    )
    exact = grant(item.id, ("records/private.md",))
    install_matches(
        monkeypatch,
        (
            match(item, "intent.md", rank=2, **exported),
            match(item, "records/private.md", rank=1, **private),
        ),
    )

    result = build_context(
        "unused.db",
        (item,),
        request(named=(item.id,), grants=(exact,)),
        now=NOW,
        receipt_dir=tmp_path / "receipts",
    )
    assert result.result == "ok"
    assert result.selected_vaults == (item.id,)
    assert [(source.path, source.access) for source in result.sources] == [
        ("intent.md", "export"),
        ("records/private.md", "session-grant"),
    ]
    assert result.sources[1].grant_sha256 == exact.sha256
    assert result.sources[0].grant_sha256 is None
    assert all(source.content_sha256 in {exported["content_sha256"], private["content_sha256"]} for source in result.sources)
    assert (tmp_path / "receipts" / f"{result.pack_id}.json").is_file()


def test_current_contract_intent_only_stage_is_evidence_insufficient(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "alpha"
    metadata = write(root, "intent.md", "# Focus\nalpha current intent")
    item = manifest(root, "vault_alpha", topics=("alpha",))
    install_matches(monkeypatch, (match(item, "intent.md", **metadata),))

    result = build_context("unused.db", (item,), request(named=(item.id,)), now=NOW)
    assert result.result == "evidence-insufficient"
    assert len(result.sources) == 1
    assert result.sources[0].path == "intent.md"


def test_source_token_excerpt_and_vault_budgets_are_hard_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vaults = []
    matches = []
    for index in range(3):
        root = tmp_path / str(index)
        metadata = write(root, "evidence.md", "alpha " + "word " * 1_600)
        item = manifest(
            root,
            f"vault_{chr(ord('a') + index)}",
            intent="intent.md",
            topics=("alpha",),
            search=("evidence.md",),
            exports=("evidence.md",),
        )
        vaults.append(item)
        matches.append(match(item, "evidence.md", rank=3 - index, **metadata))
    install_matches(monkeypatch, tuple(matches))

    result = build_context(
        "unused.db",
        tuple(vaults),
        request(budget=Budget(source_tokens=1_600, vaults=2, excerpts=1)),
        now=NOW,
    )
    assert len(result.sources) == 1
    assert result.sources[0].source_tokens == 1_500
    assert sum(source.source_tokens for source in result.sources) <= 1_600
    assert result.selected_vaults == ("vault_a", "vault_b")
    assert result.considered_and_dropped == ({"vault": "vault_c", "reason": "vault budget reached"},)


def test_cutoff_stops_after_two_vaults_add_no_new_requested_facet_coverage(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    vaults = []
    matches = []
    for name in ("a", "b", "c"):
        root = tmp_path / name
        metadata = write(root, "search-only.md", f"secret-{name} alpha")
        item = manifest(
            root,
            f"vault_{name}",
            topics=("alpha",),
            search=("search-only.md",),
            exports=(),
        )
        vaults.append(item)
        matches.append(match(item, "search-only.md", **metadata, search_only=True, exported=False))
    install_matches(monkeypatch, tuple(matches))

    result = build_context("unused.db", tuple(vaults), request(), now=NOW)
    assert result.selected_vaults == ("vault_a", "vault_b")
    assert result.considered_and_dropped == (
        {"vault": "vault_c", "reason": "no new requested-facet coverage above cutoff"},
    )


def test_named_vault_bypasses_empty_coverage_cutoff(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    named_root = tmp_path / "named"
    metadata = write(named_root, "evidence.md", "alpha proof")
    named = manifest(
        named_root,
        "vault_z",
        topics=("alpha",),
        intent="intent.md",
        search=("evidence.md",),
        exports=("evidence.md",),
    )
    empties = tuple(manifest(tmp_path / key, f"vault_{key}", topics=("alpha",)) for key in ("a", "b"))
    install_matches(monkeypatch, (match(named, "evidence.md", **metadata),))
    result = build_context(
        "unused.db", (*empties, named), request(named=(named.id,), budget=Budget(vaults=3)), now=NOW
    )
    assert named.id in result.selected_vaults


def test_search_only_match_receipts_disclose_no_path_term_score_facet_or_count_signal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "alpha"
    first = write(root, "private/secret-term.md", "classified-facet alpha")
    second = write(root, "private/other.md", "different hidden words")
    item = manifest(
        root,
        "vault_alpha",
        topics=("alpha",),
        search=("private/secret-term.md", "private/other.md"),
        exports=(),
    )
    install_matches(
        monkeypatch,
        (
            match(item, "private/secret-term.md", rank=999, **first, search_only=True, exported=False),
            match(item, "private/other.md", rank=0.1, **second, search_only=True, exported=False),
        ),
    )
    result = build_context("unused.db", (item,), request(intent="classified-facet", facets=("classified-facet",)), now=NOW)
    assert result.sources == ()
    assert result.excluded == (
        retrieval.ExclusionReceipt(
            vault=item.id,
            path=None,
            reason="matching content is not disclosed",
            code=ErrorCode.GRANT_MISSING.value,
        ),
    )
    # The digests cover the manifest, whose root is tmp_path, so they change every run and in about
    # 3% of runs one contains "999" by chance. Prove they are bare hex, then scan every other field.
    assert re.fullmatch(r"[0-9a-f]{64}", result.policy_sha256)
    assert re.fullmatch(r"[0-9a-f]{64}", result.pack_sha256)
    assert result.pack_id == f"pack_{result.pack_sha256[:16]}"
    rendered = str(replace(result, policy_sha256="", pack_sha256="", pack_id="").as_dict())
    for secret in ("private/secret-term.md", "private/other.md", "classified-facet", "999", "0.1"):
        assert secret not in rendered


@pytest.mark.parametrize("code", [ErrorCode.INDEX_MISSING, ErrorCode.INDEX_STALE])
def test_index_failure_degrades_to_exact_grant_paths_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: ErrorCode
) -> None:
    root = tmp_path / "alpha"
    write(root, "private.md", "alpha durable evidence")
    item = manifest(root, "vault_alpha", intent="intent.md", exports=())
    exact = grant(item.id, ("private.md",))

    def unavailable(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise BrainError(code, "synthetic index failure")

    monkeypatch.setattr(retrieval, "_query", unavailable)
    result = build_context("missing.db", (item,), request(grants=(exact,)), now=NOW)
    assert result.result == "ok"
    assert [(source.path, source.access) for source in result.sources] == [("private.md", "session-grant")]
    assert result.warnings == (f"degraded exact-grant read: {code.value}",)


@pytest.mark.parametrize("code", [ErrorCode.INDEX_MISSING, ErrorCode.NOT_FOUND])
def test_index_failure_without_an_applicable_degraded_route_is_raised(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: ErrorCode
) -> None:
    item = manifest(tmp_path, "vault_alpha")

    def unavailable(*_args: object, **_kwargs: object) -> tuple[object, ...]:
        raise BrainError(code, "synthetic index failure")

    monkeypatch.setattr(retrieval, "_query", unavailable)
    grants = () if code is ErrorCode.INDEX_MISSING else (grant(item.id, ("private.md",)),)
    with pytest.raises(BrainError) as caught:
        build_context("missing.db", (item,), request(grants=grants), now=NOW)
    assert caught.value.code is code


def test_grant_fallback_ignores_foreign_sessions_unknown_vaults_and_existing_matches(
    tmp_path: Path,
) -> None:
    item = manifest(tmp_path, "vault_alpha")
    existing = match(item, "private.md")
    additions = retrieval._grant_fallbacks(
        request(
            grants=(
                grant(item.id, ("private.md", "extra.md")),
                grant(item.id, ("wrong-session.md",), session_id="other"),
                grant("vault_missing", ("missing.md",)),
            )
        ),
        (item,),
        (existing,),
    )
    assert [entry["path"] for entry in additions] == ["private.md", "extra.md"]


def test_hash_mismatch_and_read_race_become_explicit_source_exclusions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "alpha"
    metadata = write(root, "evidence.md", "alpha stable content")
    item = manifest(root, "vault_alpha", intent="intent.md", exports=("evidence.md",), search=("evidence.md",))
    stale = {**metadata, "content_sha256": "0" * 64}
    install_matches(monkeypatch, (match(item, "evidence.md", **stale),))
    mismatch = build_context("unused.db", (item,), request(), now=NOW)
    assert mismatch.sources == ()
    assert [(entry.path, entry.code) for entry in mismatch.excluded] == [
        ("evidence.md", ErrorCode.SOURCE_CHANGED.value)
    ]

    def raced(*_args: object, **_kwargs: object) -> FileSnapshot:
        raise BrainError(ErrorCode.SOURCE_CHANGED, "synthetic source race")

    monkeypatch.setattr(retrieval, "read_snapshot", raced)
    raced_result = build_context("unused.db", (item,), request(), now=NOW)
    assert raced_result.excluded[0].reason == "synthetic source race"


def test_missing_blank_binary_and_policy_excluded_sources_are_receipted_without_stopping(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = tmp_path / "alpha"
    blank = write(root, "blank.md", "\n\n")
    binary = write(root, "binary.md", b"alpha\x00secret")
    item = manifest(
        root,
        "vault_alpha",
        topics=("alpha",),
        exports=("blank.md", "binary.md", "missing.md"),
        search=("blank.md", "binary.md", "missing.md", "search-only.md"),
    )
    install_matches(
        monkeypatch,
        (
            match(item, "blank.md", rank=4, **blank),
            match(item, "binary.md", rank=3, **binary),
            match(item, "missing.md", rank=2),
            match(item, "search-only.md", rank=1, search_only=True, exported=False),
        ),
    )
    result = build_context("unused.db", (item,), request(), now=NOW)
    assert result.sources == ()
    assert {entry.code for entry in result.excluded} == {
        ErrorCode.SOURCE_UNREADABLE.value,
        ErrorCode.SOURCE_BINARY.value,
        ErrorCode.PATH_MISSING.value,
        ErrorCode.GRANT_MISSING.value,
    }


def test_no_matches_produces_evidence_insufficient_pack_without_selected_vaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    install_matches(monkeypatch, ())
    item = manifest(tmp_path, "vault_alpha")
    result = build_context("unused.db", (item,), request(facets=("unrelated",)), now=NOW)
    assert result.result == "evidence-insufficient"
    assert result.selected_vaults == ()
    assert result.sources == ()
