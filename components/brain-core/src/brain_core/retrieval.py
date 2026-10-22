"""Deterministic ranking, policy gating, cutoff, and excerpt assembly."""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from brain_core.contracts import (
    PROTOCOL_VERSION,
    TOKENIZER_ID,
    ContextPack,
    ContextRequest,
    ExclusionReceipt,
    SourceReceipt,
    VaultManifest,
)
from brain_core.errors import BrainError, ErrorCode
from brain_core.paths import read_snapshot
from brain_core.policy import check_read_access, policy_sha256
from brain_core.provenance import locator_for_line, make_pack, save_pack

_TOKEN = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_WORD = re.compile(r"[\w][\w-]*", re.UNICODE)
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how",
    "i", "in", "is", "it", "my", "of", "on", "or", "that", "the", "this",
    "to", "use", "what", "with",
}


@dataclass(frozen=True, slots=True)
class RankedVault:
    manifest: VaultManifest
    score: float
    named: bool
    matches: tuple[Any, ...]


def tokenize(text: str) -> tuple[str, ...]:
    return tuple(_TOKEN.findall(text))


def token_count(text: str) -> int:
    return len(tokenize(text))


def derive_facets(intent: str, *, limit: int = 12) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for match in _WORD.finditer(intent.casefold()):
        term = match.group(0)
        if term in _STOPWORDS or term in seen:
            continue
        seen.add(term)
        result.append(term)
        if len(result) == limit:
            break
    return tuple(result)


def requested_facets(request: ContextRequest) -> tuple[str, ...]:
    base = request.requested_facets or derive_facets(request.intent)
    if request.expansion_facet is None:
        return base
    expansion = request.expansion_facet.casefold().strip()
    return base if not expansion or expansion in base else (*base, expansion)


def _match_value(match: Any, name: str, default: Any = None) -> Any:
    if isinstance(match, dict):
        return match.get(name, default)
    return getattr(match, name, default)


def _topic_score(manifest: VaultManifest, facets: tuple[str, ...]) -> float:
    topic_text = " ".join((manifest.name, manifest.purpose, *manifest.topics)).casefold()
    hits = sum(1 for facet in facets if facet in topic_text)
    return min(30.0, hits * 20.0)


def rank_vaults(
    manifests: tuple[VaultManifest, ...],
    matches: Iterable[Any],
    request: ContextRequest,
) -> tuple[RankedVault, ...]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for match in matches:
        grouped[str(_match_value(match, "vault_id"))].append(match)
    facets = requested_facets(request)
    manifest_by_id = {manifest.id: manifest for manifest in manifests}
    missing = sorted(set(request.named_vaults) - manifest_by_id.keys())
    if missing:
        raise BrainError(
            ErrorCode.NOT_FOUND,
            "A named vault was not discovered",
            details={"missing_vaults": missing},
        )
    if len(request.named_vaults) > request.budget.vaults:
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            "Named vault count exceeds the vault budget",
            details={"named": len(request.named_vaults), "budget": request.budget.vaults},
        )
    dependencies = {
        dependency
        for vault_id in request.named_vaults
        for dependency in manifest_by_id[vault_id].dependencies
    }
    ranked: list[RankedVault] = []
    for manifest in manifests:
        named = manifest.id in request.named_vaults
        internal_matches = tuple(
            sorted(
                grouped.get(manifest.id, ()),
                key=lambda item: (-float(_match_value(item, "rank", 0.0)), str(_match_value(item, "path", ""))),
            )
        )
        fts_score = min(20.0, max((float(_match_value(item, "rank", 0.0)) for item in internal_matches), default=0.0))
        score = (100.0 if named else 0.0) + (30.0 if manifest.id in dependencies else 0.0)
        score += _topic_score(manifest, facets) + fts_score
        if score > 0.0 or internal_matches:
            ranked.append(RankedVault(manifest=manifest, score=score, named=named, matches=internal_matches))
    return tuple(sorted(ranked, key=lambda item: (-item.score, item.manifest.id)))


def _blocks(text: str) -> tuple[tuple[int, str], ...]:
    lines = text.splitlines()
    blocks: list[tuple[int, str]] = []
    start = 0
    current: list[str] = []
    for index, line in enumerate(lines):
        if not line.strip():
            if current:
                blocks.append((start, "\n".join(current).strip()))
                current = []
            start = index + 1
            continue
        if not current:
            start = index
        current.append(line)
    if current:
        blocks.append((start, "\n".join(current).strip()))
    return tuple(blocks)


def excerpt_for_facets(text: str, facets: tuple[str, ...], *, max_tokens: int) -> tuple[str, str, frozenset[str]]:
    candidates = _blocks(text)
    if not candidates:
        return "", "line:1", frozenset()
    scored: list[tuple[int, int, str, frozenset[str]]] = []
    for line_index, block in candidates:
        lowered = block.casefold()
        coverage = frozenset(facet for facet in facets if facet in lowered)
        scored.append((-len(coverage), line_index, block, coverage))
    scored.sort(key=lambda item: (item[0], item[1]))
    chosen: list[tuple[int, str, frozenset[str]]] = []
    used = 0
    covered: set[str] = set()
    for _, line_index, block, coverage in scored:
        count = token_count(block)
        if chosen and used + count > max_tokens:
            continue
        if not chosen and count > max_tokens:
            words = tokenize(block)[:max_tokens]
            block = " ".join(words)
            count = len(words)
        chosen.append((line_index, block, coverage))
        used += count
        covered.update(coverage)
        if used >= max_tokens or covered.issuperset(facets):
            break
    chosen.sort(key=lambda item: item[0])
    excerpt = "\n\n".join(item[1] for item in chosen)
    return excerpt, locator_for_line(text, chosen[0][0]), frozenset(covered)


def _query(index_path: str | Path, intent: str, allow_stale: bool) -> tuple[Any, ...]:
    from brain_core.index import query_index

    return query_index(index_path, intent, allow_stale=allow_stale, limit=200)


def _grant_fallbacks(request: ContextRequest, manifests: tuple[VaultManifest, ...], matches: tuple[Any, ...]) -> tuple[Any, ...]:
    present = {(str(_match_value(item, "vault_id")), str(_match_value(item, "path"))) for item in matches}
    manifest_by_id = {manifest.id: manifest for manifest in manifests}
    additions: list[dict[str, Any]] = []
    for grant in request.grants:
        manifest = manifest_by_id.get(grant.vault)
        if manifest is None or grant.session_id != request.session_id:
            continue
        for path in grant.paths:
            if (grant.vault, path) not in present:
                additions.append(
                    {
                        "vault_id": grant.vault,
                        "path": path,
                        "root": manifest.root,
                        "content_sha256": None,
                        "size": 0,
                        "search_only": False,
                        "exported": False,
                        "rank": 0.0,
                    }
                )
    return (*matches, *additions)


def plan_context(
    index_path: str | Path,
    manifests: tuple[VaultManifest, ...],
    request: ContextRequest,
    *,
    allow_stale: bool = False,
) -> dict[str, Any]:
    """Return a body-free deterministic plan before any excerpt read."""

    try:
        matches = _query(index_path, request.intent, allow_stale)
    except BrainError as exc:
        if exc.code not in {ErrorCode.INDEX_MISSING, ErrorCode.INDEX_STALE} or not request.grants:
            raise
        matches = ()
    matches = _grant_fallbacks(request, manifests, matches)
    ranked = rank_vaults(manifests, matches, request)
    planned = tuple(item.manifest.id for item in ranked[: request.budget.vaults])
    return {
        "intent": request.intent,
        "requested_facets": list(requested_facets(request)),
        "named_vaults": list(request.named_vaults),
        "planned_vaults": list(planned),
        "budget": {
            "source_tokens": request.budget.source_tokens,
            "vaults": request.budget.vaults,
            "excerpts": request.budget.excerpts,
        },
        "grant_sha256": sorted(grant.sha256 for grant in request.grants),
        "tokenizer": TOKENIZER_ID,
        "policy_sha256": policy_sha256(
            manifests,
            request.grants,
            node=request.node,
            mode=request.mode,
            session_id=request.session_id,
        ),
        "note": "Final cutoff occurs only after permitted excerpt coverage is measured.",
    }


def build_context(
    index_path: str | Path,
    manifests: tuple[VaultManifest, ...],
    request: ContextRequest,
    *,
    now: datetime,
    receipt_dir: str | Path | None = None,
    allow_stale: bool = False,
) -> ContextPack:
    # index candidate -> policy/grant gate -> cutoff -> excerpt -> provenance receipt
    #       metadata              no read             bounded         stable hash
    warnings: list[str] = []
    try:
        matches = _query(index_path, request.intent, allow_stale)
    except BrainError as exc:
        if exc.code not in {ErrorCode.INDEX_MISSING, ErrorCode.INDEX_STALE} or not request.grants:
            raise
        matches = ()
        warnings.append(f"degraded exact-grant read: {exc.code.value}")
    matches = _grant_fallbacks(request, manifests, matches)
    ranked = rank_vaults(manifests, matches, request)
    facets = requested_facets(request)
    selected: list[str] = []
    dropped: list[dict[str, str]] = []
    sources: list[SourceReceipt] = []
    excluded: list[ExclusionReceipt] = []
    covered: set[str] = set()
    total_tokens = 0
    empty_streak = 0
    evidence_only_intent = True
    for candidate in ranked:
        if len(selected) >= request.budget.vaults:
            dropped.append({"vault": candidate.manifest.id, "reason": "vault budget reached"})
            continue
        if empty_streak >= 2 and not candidate.named:
            dropped.append({"vault": candidate.manifest.id, "reason": "no new requested-facet coverage above cutoff"})
            continue
        selected.append(candidate.manifest.id)
        before_coverage = set(covered)
        redacted_exclusion_added = False
        if not candidate.matches:
            excluded.append(
                ExclusionReceipt(candidate.manifest.id, None, "no permitted indexed evidence", ErrorCode.NOT_FOUND.value)
            )
        for match in candidate.matches:
            if len(sources) >= request.budget.excerpts or total_tokens >= request.budget.source_tokens:
                break
            path = str(_match_value(match, "path"))
            decision = check_read_access(
                candidate.manifest,
                path,
                request.grants,
                now,
                request.session_id,
                request.node,
                request.mode,
            )
            if not decision.allowed:
                if not redacted_exclusion_added:
                    excluded.append(
                        ExclusionReceipt(
                            candidate.manifest.id,
                            None,
                            "matching content is not disclosed",
                            (decision.code or ErrorCode.POLICY_EXCLUDED).value,
                        )
                    )
                    redacted_exclusion_added = True
                continue
            remaining = request.budget.source_tokens - total_tokens
            per_source = min(1_500, remaining)
            try:
                snapshot = read_snapshot(
                    candidate.manifest.root,
                    path,
                    max_bytes=max(20_000, int(_match_value(match, "size", 0)) + 1),
                    expected_sha256=_match_value(match, "content_sha256"),
                )
                excerpt, locator, excerpt_coverage = excerpt_for_facets(snapshot.text, facets, max_tokens=per_source)
            except BrainError as exc:
                excluded.append(ExclusionReceipt(candidate.manifest.id, path, exc.message, exc.code.value))
                continue
            count = token_count(excerpt)
            if count == 0:
                excluded.append(ExclusionReceipt(candidate.manifest.id, path, "source contains no text", ErrorCode.SOURCE_UNREADABLE.value))
                continue
            sources.append(
                SourceReceipt(
                    vault=candidate.manifest.id,
                    path=path,
                    content_sha256=snapshot.content_sha256,
                    locator=locator,
                    access=decision.access,
                    grant_sha256=decision.grant_sha256,
                    source_tokens=count,
                    excerpt=excerpt,
                )
            )
            total_tokens += count
            covered.update(excerpt_coverage)
            if candidate.manifest.intent != path:
                evidence_only_intent = False
        empty_streak = empty_streak + 1 if covered == before_coverage else 0
    named_selected = set(request.named_vaults).issubset(selected)
    result = "ok"
    if not sources or not named_selected or evidence_only_intent:
        result = "evidence-insufficient"
    policy_hash = policy_sha256(
        manifests,
        request.grants,
        node=request.node,
        mode=request.mode,
        session_id=request.session_id,
    )
    pack = make_pack(
        protocol_version=PROTOCOL_VERSION,
        request_id=request.request_id,
        result=result,
        selected_vaults=tuple(selected),
        considered_and_dropped=tuple(dropped),
        sources=sources,
        excluded=excluded,
        warnings=tuple(warnings),
        tokenizer=TOKENIZER_ID,
        policy_sha256=policy_hash,
    )
    if receipt_dir is not None:
        save_pack(pack, receipt_dir)
    return pack
