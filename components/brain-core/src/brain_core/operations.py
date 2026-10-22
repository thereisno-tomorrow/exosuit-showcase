"""Business-operation dispatcher shared by the human and JSON CLIs."""

from __future__ import annotations

import sqlite3
import sys
from contextlib import closing
from dataclasses import asdict
from datetime import UTC, datetime
from math import isfinite
from typing import Any, Callable, Mapping

from brain_core import __version__
from brain_core import language
from brain_core.clearout import plan_clearout
from brain_core.contracts import (
    Budget,
    ContextRequest,
    Grant,
    VaultManifest,
    grant_from_mapping,
    parse_yaml,
)
from brain_core.discovery import discover_catalog, discover_legacy_registry
from brain_core.durability import SharedDurabilityWriter
from brain_core.errors import BrainError, ErrorCode
from brain_core.index import index_status, rebuild_index
from brain_core.continuity import append_event, close_continuity, recover_continuity, start_continuity
from brain_core.migration import import_decisions
from brain_core.paths import read_local_text
from brain_core.protocol import ProtocolRequest
from brain_core.provenance import load_pack
from brain_core.records import RECORD_SCHEMA_V2, Evidence, RecordStore, evidence_from_mapping, record_from_mapping
from brain_core.retrieval import build_context, plan_context
from brain_core.timewords import resolve as resolve_time_words, today_in_singapore
from brain_core.skills import (
    SkillCatalog,
    SkillRouteRequest,
    load_selected_instructions,
    route_skill,
)


def _required(parameters: Mapping[str, Any], name: str, expected: type[Any]) -> Any:
    value = parameters.get(name)
    if not isinstance(value, expected) or (expected is str and not value):
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            f"{name} is required",
            details={"field": name, "expected": expected.__name__},
        )
    return value


def _strings(parameters: Mapping[str, Any], name: str) -> tuple[str, ...]:
    value = parameters.get(name, [])
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            f"{name} must be a list of non-empty strings",
            details={"field": name},
        )
    return tuple(value)


def _positive(parameters: Mapping[str, Any], name: str, default: int) -> int:
    value = parameters.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            f"{name} must be a positive integer",
            details={"field": name, "actual": value},
        )
    return value


def _nonnegative_float(parameters: Mapping[str, Any], name: str, default: float) -> float:
    value = parameters.get(name, default)
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not isfinite(value)
        or value < 0
    ):
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            f"{name} must be a finite non-negative number",
            details={"field": name, "actual": value},
        )
    return float(value)


def _boolean(parameters: Mapping[str, Any], name: str, default: bool = False) -> bool:
    value = parameters.get(name, default)
    if not isinstance(value, bool):
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            f"{name} must be a boolean",
            details={"field": name, "actual": value},
        )
    return value


def _optional_string(
    parameters: Mapping[str, Any],
    name: str,
    default: str | None = None,
) -> str | None:
    value = parameters.get(name, default)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            f"{name} must be a non-empty string",
            details={"field": name, "actual": value},
        )
    return value


def _moment(parameters: Mapping[str, Any]) -> datetime:
    value = parameters.get("now")
    if value is None:
        return datetime.now(UTC)
    if not isinstance(value, str):
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "now must be an ISO timestamp", details={"field": "now"})
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "now must be an ISO timestamp", details={"field": "now", "value": value}) from exc
    if parsed.tzinfo is None:
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "now requires a timezone", details={"field": "now", "value": value})
    return parsed


def _input_mapping(
    parameters: Mapping[str, Any],
    inline_name: str,
    file_name: str,
) -> dict[str, Any]:
    inline = parameters.get(inline_name)
    file_path = parameters.get(file_name)
    if (inline is None) == (file_path is None):
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            f"Provide exactly one of {inline_name} or {file_name}",
            details={inline_name: inline is not None, file_name: file_path is not None},
        )
    if file_path is not None:
        if not isinstance(file_path, str) or not file_path:
            raise BrainError(ErrorCode.INVALID_ARGUMENT, f"{file_name} must be a path", details={"field": file_name})
        return parse_yaml(read_local_text(file_path), file_path)
    if not isinstance(inline, Mapping):
        raise BrainError(ErrorCode.INVALID_ARGUMENT, f"{inline_name} must be an object", details={"field": inline_name})
    return dict(inline)


def _optional_input_mapping(
    parameters: Mapping[str, Any],
    inline_name: str,
    file_name: str,
) -> dict[str, Any] | None:
    if parameters.get(inline_name) is None and parameters.get(file_name) is None:
        return None
    return _input_mapping(parameters, inline_name, file_name)


def _record_store(parameters: Mapping[str, Any]) -> RecordStore:
    return RecordStore(_required(parameters, "brain_home", str))


def _evidence(parameters: Mapping[str, Any]) -> Evidence:
    value = _input_mapping(parameters, "evidence", "evidence_file")
    return evidence_from_mapping(value, "operation evidence")


def _grants(parameters: Mapping[str, Any]) -> tuple[Grant, ...]:
    grants: list[Grant] = []
    seen: set[str] = set()
    for path in _strings(parameters, "grant_files"):
        grant = grant_from_mapping(parse_yaml(read_local_text(path), path), path)
        if grant.id in seen:
            raise BrainError(ErrorCode.DUPLICATE_ID, f"Duplicate grant ID: {grant.id}", details={"grant_id": grant.id})
        seen.add(grant.id)
        grants.append(grant)
    return tuple(sorted(grants, key=lambda item: item.id))


def _catalog(parameters: Mapping[str, Any]) -> tuple[str, bool, str, tuple[VaultManifest, ...]]:
    node_map = parameters.get("node_map")
    legacy = parameters.get("legacy_registry")
    if bool(node_map) == bool(legacy):
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            "Provide exactly one of node_map or legacy_registry",
            details={"node_map": node_map, "legacy_registry": legacy},
        )
    if node_map:
        if not isinstance(node_map, str):
            raise BrainError(ErrorCode.INVALID_ARGUMENT, "node_map must be a path", details={"field": "node_map"})
        loaded, manifests = discover_catalog(node_map)
        return node_map, False, loaded.node, manifests
    if not isinstance(legacy, str):
        raise BrainError(ErrorCode.INVALID_ARGUMENT, "legacy_registry must be a path", details={"field": "legacy_registry"})
    loaded, manifests = discover_legacy_registry(
        legacy,
        node=_optional_string(parameters, "node", "legacy-local") or "legacy-local",
    )
    return legacy, True, loaded.node, manifests


def _context_request(request_id: str, parameters: Mapping[str, Any], grants: tuple[Grant, ...], node: str) -> ContextRequest:
    return ContextRequest(
        request_id=request_id,
        session_id=_required(parameters, "session_id", str),
        intent=_required(parameters, "intent", str),
        named_vaults=_strings(parameters, "vaults"),
        requested_facets=_strings(parameters, "facets"),
        mode=_optional_string(parameters, "mode", "think") or "think",
        node=_optional_string(parameters, "node", node) or node,
        grants=grants,
        budget=Budget(
            source_tokens=_positive(parameters, "source_tokens", 12_000),
            vaults=_positive(parameters, "vault_budget", 4),
            excerpts=_positive(parameters, "excerpt_budget", 12),
        ),
        expansion_facet=_optional_string(parameters, "expansion_facet"),
    )


def doctor() -> dict[str, Any]:
    supported = (3, 12) <= sys.version_info[:2] < (3, 15)
    if not supported:
        raise BrainError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "Unsupported Python version",
            details={"python": sys.version.split()[0], "supported": ">=3.12,<3.15"},
        )
    try:
        with closing(sqlite3.connect(":memory:")) as connection:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(body)")
    except sqlite3.Error as exc:
        raise BrainError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "SQLite FTS5 is unavailable",
            details={"sqlite": sqlite3.sqlite_version},
        ) from exc
    return {
        "version": __version__,
        "python": sys.version.split()[0],
        "sqlite": sqlite3.sqlite_version,
        "fts5": "ok",
        "protocol_versions": ["1"],
        "tokenizer": "brain.lexical/v1",
        "model_calls": 0,
        "network_calls": 0,
        "phase": "2-records",
    }


def _doctor(_: ProtocolRequest) -> dict[str, Any]:
    return doctor()


def _vault_discover(request: ProtocolRequest) -> dict[str, Any]:
    _, legacy, node, manifests = _catalog(request.parameters)
    return {
        "node": node,
        "adapter": "legacy-registry" if legacy else "brain.vault/v1",
        "vaults": [manifest.as_dict() for manifest in manifests],
    }


def _index_rebuild(request: ProtocolRequest) -> dict[str, Any]:
    source, legacy, node, _ = _catalog(request.parameters)
    grants = _grants(request.parameters)
    result = rebuild_index(
        source,
        _required(request.parameters, "database", str),
        grants=grants,
        now=_moment(request.parameters),
        session_id=_optional_string(request.parameters, "session_id", "index-rebuild") or "index-rebuild",
        node=_optional_string(request.parameters, "node", node) or node,
        mode=_optional_string(request.parameters, "mode", "think") or "think",
        lock_timeout=_nonnegative_float(request.parameters, "lock_timeout", 1.0),
        legacy_registry=legacy,
    )
    return asdict(result)


def _index_status(request: ProtocolRequest) -> dict[str, Any]:
    return asdict(index_status(_required(request.parameters, "database", str)))


def _context_plan(request: ProtocolRequest) -> dict[str, Any]:
    _, _, node, manifests = _catalog(request.parameters)
    grants = _grants(request.parameters)
    context_request = _context_request(request.request_id, request.parameters, grants, node)
    return plan_context(
        _required(request.parameters, "database", str),
        manifests,
        context_request,
        allow_stale=_boolean(request.parameters, "allow_stale"),
    )


def _context_build(request: ProtocolRequest) -> dict[str, Any]:
    _, _, node, manifests = _catalog(request.parameters)
    grants = _grants(request.parameters)
    context_request = _context_request(request.request_id, request.parameters, grants, node)
    pack = build_context(
        _required(request.parameters, "database", str),
        manifests,
        context_request,
        now=_moment(request.parameters),
        receipt_dir=_optional_string(request.parameters, "receipt_dir"),
        allow_stale=_boolean(request.parameters, "allow_stale"),
    )
    return pack.as_dict()


def _context_inspect(request: ProtocolRequest) -> dict[str, Any]:
    return load_pack(
        _required(request.parameters, "receipt_dir", str),
        _required(request.parameters, "pack_id", str),
    )


def _skill_catalog(parameters: Mapping[str, Any]) -> SkillCatalog:
    return SkillCatalog.from_paths(_strings(parameters, "skill_paths"))


def _skills_list(request: ProtocolRequest) -> dict[str, Any]:
    catalog = _skill_catalog(request.parameters)
    return {"skills": [asdict(entry) for entry in catalog.entries]}


def _skills_route(request: ProtocolRequest) -> dict[str, Any]:
    catalog = _skill_catalog(request.parameters)
    route_request = SkillRouteRequest(
        intent=_required(request.parameters, "intent", str),
        mode=_optional_string(request.parameters, "mode", "think") or "think",
        explicit_skill_id=_optional_string(request.parameters, "explicit_skill_id"),
        authority=frozenset(_strings(request.parameters, "authority")),
        available_dependencies=frozenset(_strings(request.parameters, "available_dependencies")),
        host_capabilities=frozenset(_strings(request.parameters, "host_capabilities")),
        project_owner=_optional_string(request.parameters, "project_owner"),
    )
    route = route_skill(catalog, route_request)
    result = asdict(route)
    if _boolean(request.parameters, "load_instructions"):
        result["instructions"] = load_selected_instructions(catalog, route, requested=True)
    return result


def _skills_check(request: ProtocolRequest) -> dict[str, Any]:
    catalog = _skill_catalog(request.parameters)
    return {"result": "ok", "count": len(catalog.entries), "model_calls": 0, "network_calls": 0}


def _record_create(request: ProtocolRequest) -> dict[str, Any]:
    value = _input_mapping(request.parameters, "record", "record_file")
    record = record_from_mapping(value, "record.create")
    if record.schema == RECORD_SCHEMA_V2:
        actor = _required(request.parameters, "actor", str)
        language.check_change(None, record, actor=actor)
    receipt = _record_store(request.parameters).create(record)
    return {"record": record.as_dict(), "receipt": asdict(receipt)}


def _durability_write(request: ProtocolRequest) -> dict[str, Any]:
    return SharedDurabilityWriter().write(request.parameters).as_dict()


def _record_list(request: ProtocolRequest) -> dict[str, Any]:
    store = _record_store(request.parameters)
    kind = _optional_string(request.parameters, "kind")
    current = _boolean(request.parameters, "current", True)
    records = store.current(kind=kind) if current else tuple(record for record in store.all() if kind is None or record.kind == kind)
    word = _optional_string(request.parameters, "word")
    if word is not None:
        records = tuple(record for record in records if record.kind == "item" and language.plain_word(record.data) == word)
    if _boolean(request.parameters, "actionable"):
        records = language.actionable(records)
    return {"current": current, "count": len(records), "records": [record.as_dict() for record in records]}


def _record_show(request: ProtocolRequest) -> dict[str, Any]:
    return _record_store(request.parameters).get(_required(request.parameters, "record_id", str)).as_dict()


def _record_transition(request: ProtocolRequest) -> dict[str, Any]:
    record, receipt = _record_store(request.parameters).revise(
        _required(request.parameters, "record_id", str),
        now=_moment(request.parameters),
        state=_optional_string(request.parameters, "state"),
        data=_optional_input_mapping(request.parameters, "data", "data_file"),
        evidence=(_evidence(request.parameters),),
        entropy=_optional_string(request.parameters, "entropy"),
        expected_sha256=_optional_string(request.parameters, "expected_sha256"),
        actor=_optional_string(request.parameters, "actor"),
        source=_optional_input_mapping(request.parameters, "source", "source_file"),
    )
    return {"record": record.as_dict(), "receipt": asdict(receipt)}


def _time_resolve(request: ProtocolRequest) -> dict[str, Any]:
    now = _moment(request.parameters)
    today = today_in_singapore(now)
    words = _required(request.parameters, "words", str)
    return {"words": words, "today": today.isoformat(), "resolved": resolve_time_words(words, today=today)}


def _memory_clearout_plan(request: ProtocolRequest) -> dict[str, Any]:
    return plan_clearout(_required(request.parameters, "root", str)).as_dict()


def _continuity_start(request: ProtocolRequest) -> dict[str, Any]:
    result = start_continuity(
        _record_store(request.parameters),
        session_id=_required(request.parameters, "session_id", str),
        intent=_required(request.parameters, "intent", str),
        sources=_strings(request.parameters, "sources"),
        evidence=_evidence(request.parameters),
        now=_moment(request.parameters),
        next_step=_optional_string(request.parameters, "next_step", "Continue the declared intent.") or "Continue the declared intent.",
        entropy=_optional_string(request.parameters, "entropy"),
        task_id=_optional_string(request.parameters, "task_id"),
    )
    return {"record": result.record.as_dict(), "receipt": asdict(result.receipt) if result.receipt else None, "changed": result.changed}


def _continuity_append(request: ProtocolRequest) -> dict[str, Any]:
    result = append_event(
        _record_store(request.parameters),
        session_id=_required(request.parameters, "session_id", str),
        event=_required(request.parameters, "event", str),
        value=_required(request.parameters, "value", str),
        evidence=_evidence(request.parameters),
        now=_moment(request.parameters),
        meaningful=_boolean(request.parameters, "meaningful", True),
        entropy=_optional_string(request.parameters, "entropy"),
    )
    return {"record": result.record.as_dict(), "receipt": asdict(result.receipt) if result.receipt else None, "changed": result.changed}


def _continuity_close(request: ProtocolRequest) -> dict[str, Any]:
    result = close_continuity(
        _record_store(request.parameters),
        session_id=_required(request.parameters, "session_id", str),
        evidence=_evidence(request.parameters),
        now=_moment(request.parameters),
        recovered=_boolean(request.parameters, "recovered"),
        entropy=_optional_string(request.parameters, "entropy"),
    )
    return {"record": result.record.as_dict(), "receipt": asdict(result.receipt) if result.receipt else None, "changed": result.changed}


def _continuity_recover(request: ProtocolRequest) -> dict[str, Any]:
    return recover_continuity(
        _record_store(request.parameters),
        session_id=_optional_string(request.parameters, "session_id"),
        max_tokens=_positive(request.parameters, "max_tokens", 2_500),
    )


def _decisions_import(request: ProtocolRequest) -> dict[str, Any]:
    apply = _boolean(request.parameters, "apply")
    store = _record_store(request.parameters) if apply else None
    return import_decisions(
        _required(request.parameters, "source", str),
        store,
        dry_run=not apply,
    ).as_dict()


_HANDLERS: dict[str, Callable[[ProtocolRequest], dict[str, Any]]] = {
    "doctor": _doctor,
    "vault.discover": _vault_discover,
    "index.rebuild": _index_rebuild,
    "index.status": _index_status,
    "context.plan": _context_plan,
    "context.build": _context_build,
    "context.inspect": _context_inspect,
    "skills.list": _skills_list,
    "skills.route": _skills_route,
    "skills.check": _skills_check,
    "record.create": _record_create,
    "durability.write": _durability_write,
    "record.list": _record_list,
    "record.show": _record_show,
    "record.transition": _record_transition,
    "time.resolve": _time_resolve,
    "memory.clearout.plan": _memory_clearout_plan,
    "continuity.start": _continuity_start,
    "continuity.append": _continuity_append,
    "continuity.close": _continuity_close,
    "continuity.recover": _continuity_recover,
    "decisions.import": _decisions_import,
}


def dispatch(request: ProtocolRequest) -> dict[str, Any]:
    handler = _HANDLERS.get(request.operation)
    if handler is None:
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            f"Unknown operation: {request.operation}",
            details={"operation": request.operation, "supported": sorted(_HANDLERS)},
        )
    return handler(request)
