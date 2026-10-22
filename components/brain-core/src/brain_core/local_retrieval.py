"""Selected A02 local retrieval: FTS5 plus exact, typed one-hop links."""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from contextlib import closing
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Mapping, cast

from brain_core.contracts import Grant, VaultManifest, grant_from_mapping, parse_yaml, stable_hash
from brain_core.errors import BrainError, ErrorCode
from brain_core.paths import normalize_relative, read_local_text, read_snapshot
from brain_core.policy import check_read_access, check_search_access
from brain_core.retrieval import derive_facets, excerpt_for_facets, token_count

CATALOG_SCHEMA = "brain.a02-retrieval-catalog/v1"
RELATION_TYPES = frozenset({"supports", "supersedes", "decides", "commits"})
EVIDENCE_STATES = frozenset({"current", "superseded", "stale", "withdrawn", "forbidden"})

RelationType = Literal["supports", "supersedes", "decides", "commits"]
RetrievalMethod = Literal["fts5", "fts5+typed-links"]

_CATALOG_FIELDS = frozenset({"schema", "repository_version", "evidence", "links"})
_EVIDENCE_FIELDS = frozenset(
    {"evidence_id", "owner_vault", "relative_path", "content_sha256", "state"}
)
_LINK_FIELDS = frozenset({"source", "target", "type"})
_EVIDENCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_TERM = re.compile(r"[^\W_]+", re.UNICODE)


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    evidence_id: str
    owner_vault: str
    relative_path: str
    content_sha256: str
    state: str


@dataclass(frozen=True, slots=True)
class TypedRelation:
    source_id: str
    target_id: str
    relation_type: RelationType


@dataclass(frozen=True, slots=True)
class RetrievalCatalog:
    repository_version: str
    evidence: tuple[EvidenceRecord, ...]
    links: tuple[TypedRelation, ...]


@dataclass(frozen=True, slots=True)
class SourceEvidence:
    evidence_id: str
    owner_vault: str
    repository_version: str
    relative_path: str
    content_sha256: str
    access_basis: str
    retrieval_method: RetrievalMethod
    locator: str
    source_tokens: int
    excerpt: str


@dataclass(frozen=True, slots=True)
class RetrievalExclusion:
    code: str
    reason: str
    count: int


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    sources: tuple[SourceEvidence, ...]
    relations: tuple[TypedRelation, ...]
    ingested_ids: tuple[str, ...]
    exclusions: tuple[RetrievalExclusion, ...]
    result_sha256: str


def _contract(message: str, **details: object) -> None:
    raise BrainError(ErrorCode.CONTRACT_INVALID, message, details=dict(details))


def _exact_fields(value: Mapping[str, Any], expected: frozenset[str], field: str) -> None:
    actual = frozenset(value)
    if actual != expected:
        _contract(
            f"{field} fields differ from {CATALOG_SCHEMA}",
            field=field,
            missing=sorted(expected - actual),
            unknown=sorted(actual - expected),
        )


def _strict_json(text: str, locator: str) -> object:
    def object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                _contract("Retrieval catalog contains a duplicate key", locator=locator, key=key)
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=object_pairs)
    except json.JSONDecodeError as exc:
        _contract("Retrieval catalog is invalid JSON", locator=locator, line=exc.lineno)


def parse_catalog(text: str, locator: str = "retrieval-catalog") -> RetrievalCatalog:
    """Strictly parse the selected A02 catalog and reject all schema drift."""

    try:
        raw = (
            _strict_json(text, locator)
            if locator.casefold().endswith(".json")
            else parse_yaml(text, locator)
        )
    except BrainError as exc:
        if exc.code is ErrorCode.CONTRACT_INVALID:
            raise
        _contract("Retrieval catalog could not be parsed", locator=locator, cause=exc.code.value)
    if not isinstance(raw, Mapping):
        _contract("Retrieval catalog must be an object", locator=locator)
    document = dict(raw)
    _exact_fields(document, _CATALOG_FIELDS, "catalog")
    if document["schema"] != CATALOG_SCHEMA:
        _contract("Unknown retrieval catalog schema", schema=document["schema"])
    version = document["repository_version"]
    if not isinstance(version, str) or not version.strip():
        _contract("repository_version must be a non-empty string")
    raw_evidence = document["evidence"]
    raw_links = document["links"]
    if not isinstance(raw_evidence, list) or not isinstance(raw_links, list):
        _contract("evidence and links must be arrays")

    evidence: list[EvidenceRecord] = []
    seen_ids: set[str] = set()
    seen_paths: set[tuple[str, str]] = set()
    for position, value in enumerate(raw_evidence):
        if not isinstance(value, Mapping):
            _contract("evidence entry must be an object", position=position)
        item = dict(value)
        _exact_fields(item, _EVIDENCE_FIELDS, f"evidence[{position}]")
        evidence_id = item["evidence_id"]
        owner = item["owner_vault"]
        digest = item["content_sha256"]
        state = item["state"]
        if not isinstance(evidence_id, str) or _EVIDENCE_ID.fullmatch(evidence_id) is None:
            _contract("Invalid evidence_id", position=position)
        if not isinstance(owner, str) or not owner.strip():
            _contract("Invalid owner_vault", position=position)
        if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
            _contract("Invalid content_sha256", position=position)
        if not isinstance(state, str) or state not in EVIDENCE_STATES:
            _contract("Invalid evidence state", position=position, state=state)
        relative = normalize_relative(cast(str, item["relative_path"]))
        if evidence_id in seen_ids:
            _contract("Duplicate evidence_id", evidence_id=evidence_id)
        path_key = (owner, relative)
        if path_key in seen_paths:
            _contract("Duplicate evidence owner/path", owner_vault=owner, relative_path=relative)
        seen_ids.add(evidence_id)
        seen_paths.add(path_key)
        evidence.append(EvidenceRecord(evidence_id, owner, relative, digest, state))

    links: list[TypedRelation] = []
    seen_links: set[tuple[str, str, str]] = set()
    for position, value in enumerate(raw_links):
        if not isinstance(value, Mapping):
            _contract("link entry must be an object", position=position)
        item = dict(value)
        _exact_fields(item, _LINK_FIELDS, f"links[{position}]")
        source, target, relation = item["source"], item["target"], item["type"]
        if source not in seen_ids or target not in seen_ids:
            _contract("Typed-link endpoint is absent", position=position)
        if relation not in RELATION_TYPES:
            _contract("Typed-link relation is not allowlisted", position=position, relation=relation)
        identity = (cast(str, source), cast(str, target), cast(str, relation))
        if identity in seen_links:
            _contract("Duplicate typed link", position=position)
        seen_links.add(identity)
        links.append(TypedRelation(identity[0], identity[1], cast(RelationType, identity[2])))
    return RetrievalCatalog(version, tuple(evidence), tuple(links))


def _validate_grants(grants: tuple[Grant, ...]) -> tuple[Grant, ...]:
    if type(grants) is not tuple:
        _contract("grants must be an exact tuple")
    validated: list[Grant] = []
    ids: set[str] = set()
    for position, grant in enumerate(grants):
        if type(grant) is not Grant:
            _contract("grant has the wrong nominal type", position=position)
        payload = grant.as_dict()
        payload["paths"] = list(grant.paths)
        checked = grant_from_mapping(payload, f"grants[{position}]")
        if checked.id in ids:
            _contract("Duplicate grant ID", grant_id=checked.id)
        ids.add(checked.id)
        validated.append(checked)
    return tuple(validated)


def _query_terms(intent: str) -> tuple[str, ...]:
    if not isinstance(intent, str):
        _contract("intent must be a string")
    return tuple(dict.fromkeys(match.group(0).casefold() for match in _TERM.finditer(intent)))


def _exclusions(counts: Counter[tuple[str, str]]) -> tuple[RetrievalExclusion, ...]:
    return tuple(
        RetrievalExclusion(code, reason, count)
        for (code, reason), count in sorted(counts.items())
    )


def retrieve(
    catalog_path: str | Path,
    manifests: tuple[VaultManifest, ...],
    intent: str,
    grants: tuple[Grant, ...],
    *,
    now: datetime,
    session_id: str,
    node: str = "local",
    mode: str = "think",
    expand_links: bool = True,
    result_limit: int = 12,
    per_source_tokens: int = 500,
) -> RetrievalResult:
    """Retrieve current, permitted evidence without touching denied source bodies."""

    if type(manifests) is not tuple or not manifests:
        _contract("manifests must be a non-empty exact tuple")
    if type(result_limit) is not int or not 1 <= result_limit <= 100:
        _contract("result_limit must be an integer from 1 through 100")
    if type(per_source_tokens) is not int or not 1 <= per_source_tokens <= 1_500:
        _contract("per_source_tokens must be an integer from 1 through 1500")
    checked_grants = _validate_grants(grants)
    locator = str(catalog_path)
    catalog = parse_catalog(read_local_text(catalog_path, max_bytes=5_000_000), locator)
    manifest_by_id = {manifest.id: manifest for manifest in manifests}
    if len(manifest_by_id) != len(manifests):
        _contract("Duplicate manifest ID")

    counts: Counter[tuple[str, str]] = Counter()
    bodies: dict[str, str] = {}
    records: dict[str, EvidenceRecord] = {}
    search_access: dict[str, str] = {}
    for record in catalog.evidence:
        manifest = manifest_by_id.get(record.owner_vault)
        if manifest is None:
            _contract("Evidence names an undiscovered owner vault", owner_vault=record.owner_vault)
        if record.state != "current":
            counts[("brain.retrieval.not_current", "non-current evidence was not ingested")] += 1
            continue
        decision = check_search_access(
            manifest, record.relative_path, checked_grants, now, session_id, node, mode
        )
        if not decision.allowed:
            counts[
                (
                    (decision.code or ErrorCode.POLICY_EXCLUDED).value,
                    "matching evidence was not ingested",
                )
            ] += 1
            continue
        snapshot = read_snapshot(
            manifest.root,
            record.relative_path,
            max_bytes=1_000_000,
            expected_sha256=record.content_sha256,
        )
        records[record.evidence_id] = record
        bodies[record.evidence_id] = snapshot.text
        search_access[record.evidence_id] = decision.grant_sha256 or decision.access

    with closing(sqlite3.connect(":memory:")) as connection:
        connection.execute(
            "CREATE VIRTUAL TABLE evidence_fts USING fts5("
            "evidence_id UNINDEXED, body, tokenize='unicode61')"
        )
        connection.executemany(
            "INSERT INTO evidence_fts(evidence_id, body) VALUES (?, ?)",
            ((evidence_id, bodies[evidence_id]) for evidence_id in sorted(bodies)),
        )
        terms = _query_terms(intent)
        if terms:
            expression = " OR ".join(
                f'"{term.replace(chr(34), chr(34) * 2)}"' for term in terms
            )
            rows = connection.execute(
                "SELECT evidence_id, bm25(evidence_fts) FROM evidence_fts "
                "WHERE evidence_fts MATCH ? ORDER BY 2, evidence_id LIMIT ?",
                (expression, result_limit),
            ).fetchall()
            lexical_ids = [str(row[0]) for row in rows]
        else:
            lexical_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT evidence_id FROM evidence_fts ORDER BY evidence_id LIMIT ?",
                    (result_limit,),
                ).fetchall()
            ]

    lexical_set = set(lexical_ids)
    linked_ids: set[str] = set()
    if expand_links:
        for link in catalog.links:
            if link.source_id in lexical_set and link.target_id in records:
                linked_ids.add(link.target_id)
            if link.target_id in lexical_set and link.source_id in records:
                linked_ids.add(link.source_id)
    candidate_ids = (*lexical_ids, *sorted(linked_ids - lexical_set))

    sources: list[SourceEvidence] = []
    disclosed: set[str] = set()
    facets = derive_facets(intent)
    for evidence_id in candidate_ids:
        if len(sources) == result_limit:
            break
        record = records[evidence_id]
        manifest = manifest_by_id[record.owner_vault]
        decision = check_read_access(
            manifest, record.relative_path, checked_grants, now, session_id, node, mode
        )
        if not decision.allowed:
            counts[
                (
                    (decision.code or ErrorCode.POLICY_EXCLUDED).value,
                    "matching evidence was not disclosed",
                )
            ] += 1
            continue
        excerpt, source_locator, _ = excerpt_for_facets(
            bodies[evidence_id], facets, max_tokens=per_source_tokens
        )
        method: RetrievalMethod = (
            "fts5" if evidence_id in lexical_set else "fts5+typed-links"
        )
        sources.append(
            SourceEvidence(
                evidence_id,
                record.owner_vault,
                catalog.repository_version,
                record.relative_path,
                record.content_sha256,
                decision.grant_sha256 or decision.access or search_access[evidence_id],
                method,
                source_locator,
                token_count(excerpt),
                excerpt,
            )
        )
        disclosed.add(evidence_id)

    relations = tuple(
        link
        for link in catalog.links
        if link.source_id in disclosed and link.target_id in disclosed
    )
    exclusion_receipts = _exclusions(counts)
    payload = {
        "repository_version": catalog.repository_version,
        "sources": [asdict(source) for source in sources],
        "relations": [asdict(link) for link in relations],
        "ingested_ids": sorted(records),
        "exclusions": [asdict(item) for item in exclusion_receipts],
    }
    return RetrievalResult(
        tuple(sources),
        relations,
        tuple(sorted(records)),
        exclusion_receipts,
        stable_hash(payload),
    )


def to_ciel_evidence_ref(
    source: SourceEvidence,
    relations: tuple[TypedRelation, ...] = (),
) -> dict[str, object]:
    """Convert retrieved evidence to Ciel's exact metadata-only receipt shape."""

    supersedes = tuple(
        link.target_id
        for link in relations
        if link.source_id == source.evidence_id and link.relation_type == "supersedes"
    )
    return {
        "schema": "brain.ciel-evidence-ref/v1",
        "evidence_id": source.evidence_id,
        "owner_locator": f"vault://{source.owner_vault}",
        "source_locator": f"vault://{source.owner_vault}/{source.relative_path}",
        "source_version": source.repository_version,
        "content_sha256": source.content_sha256,
        "authority_basis": source.access_basis,
        "retrieval_method": source.retrieval_method,
        "supersedes": supersedes,
        "metadata_only": True,
    }
