"""Immutable Phase 1 contracts and strict YAML decoding."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Mapping, TypeVar

from ruamel.yaml import YAML
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import YAMLError

from brain_core.errors import BrainError, ErrorCode

VAULT_SCHEMA = "brain.vault/v1"
NODE_MAP_SCHEMA = "brain.node-map/v1"
GRANT_SCHEMA = "brain.grant/v1"
PROTOCOL_VERSION = "1"
TOKENIZER_ID = "brain.lexical/v1"
_ID = re.compile(r"^vault_[a-z0-9][a-z0-9_]*$")
_GRANT_ID = re.compile(r"^grant_[A-Za-z0-9][A-Za-z0-9_-]*$")
_T = TypeVar("_T")


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def parse_yaml(text: str, locator: str) -> dict[str, Any]:
    yaml = YAML(typ="safe", pure=True)
    yaml.allow_duplicate_keys = False
    try:
        value = yaml.load(text)
    except (DuplicateKeyError, YAMLError) as exc:
        mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
        details: dict[str, Any] = {"locator": locator}
        if mark is not None:
            details.update(line=mark.line + 1, column=mark.column + 1)
        raise BrainError(
            ErrorCode.YAML_INVALID,
            f"Invalid YAML in {locator}",
            details=details,
            hint="Repair the reported YAML syntax or duplicate key.",
        ) from exc
    if not isinstance(value, Mapping):
        raise BrainError(
            ErrorCode.CONTRACT_INVALID,
            f"{locator} must contain a YAML mapping",
            details={"locator": locator, "expected": "mapping"},
        )
    return dict(value)


def _mapping(value: Any, field_name: str, locator: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _contract_error(locator, field_name, "mapping", value)
    return dict(value)


def _list(value: Any, field_name: str, locator: str) -> list[Any]:
    if not isinstance(value, list):
        _contract_error(locator, field_name, "list", value)
    return value


def _string(value: Any, field_name: str, locator: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        _contract_error(locator, field_name, "non-empty string", value)
    return value


def _strings(value: Any, field_name: str, locator: str) -> tuple[str, ...]:
    return tuple(_string(item, f"{field_name}[]", locator) for item in _list(value, field_name, locator))


def _optional_strings(value: Any, field_name: str, locator: str) -> tuple[str, ...]:
    if value is None:
        return ()
    return _strings(value, field_name, locator)


def _integer(value: Any, field_name: str, locator: str, *, minimum: int = 1) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        _contract_error(locator, field_name, f"integer >= {minimum}", value)
    return value


def _one_of(value: str, allowed: set[str], field_name: str, locator: str) -> str:
    if value not in allowed:
        raise BrainError(
            ErrorCode.CONTRACT_INVALID,
            f"Invalid {field_name} in {locator}",
            details={"locator": locator, "field": field_name, "allowed": sorted(allowed), "actual": value},
        )
    return value


def _contract_error(locator: str, field_name: str, expected: str, actual: Any) -> None:
    raise BrainError(
        ErrorCode.CONTRACT_INVALID,
        f"Invalid field {field_name} in {locator}",
        details={
            "locator": locator,
            "field": field_name,
            "expected": expected,
            "actual_type": type(actual).__name__,
        },
    )


def _timestamp(value: Any, field_name: str, locator: str) -> datetime:
    raw = _string(value, field_name, locator)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BrainError(
            ErrorCode.CONTRACT_INVALID,
            f"Invalid timestamp {field_name} in {locator}",
            details={"locator": locator, "field": field_name, "value": raw},
        ) from exc
    if parsed.tzinfo is None:
        raise BrainError(
            ErrorCode.CONTRACT_INVALID,
            f"Timestamp {field_name} in {locator} needs a timezone",
            details={"locator": locator, "field": field_name, "value": raw},
        )
    return parsed


@dataclass(frozen=True, slots=True)
class ExportSpec:
    name: str
    paths: tuple[str, ...]
    max_bytes: int = 20_000


@dataclass(frozen=True, slots=True)
class SearchSpec:
    paths: tuple[str, ...]
    purpose: str = "ranking-only"


@dataclass(frozen=True, slots=True)
class VaultManifest:
    schema: str
    id: str
    name: str
    purpose: str
    topics: tuple[str, ...]
    sensitivity: str
    sync_class: str
    default_branch: str
    intent: str | None
    record_roots: tuple[str, ...]
    dependencies: tuple[str, ...]
    search: SearchSpec
    exports: tuple[ExportSpec, ...]
    root: str = field(compare=False, default="")

    @property
    def export_paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(path for export in self.exports for path in export.paths))

    @property
    def index_paths(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys((*self.search.paths, *self.export_paths)))

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def vault_manifest_from_mapping(data: Mapping[str, Any], locator: str, *, root: str = "") -> VaultManifest:
    schema = _string(data.get("schema"), "schema", locator)
    if schema != VAULT_SCHEMA:
        _contract_error(locator, "schema", VAULT_SCHEMA, schema)
    vault_id = _string(data.get("id"), "id", locator)
    if not _ID.fullmatch(vault_id):
        _contract_error(locator, "id", "vault_<lowercase-id>", vault_id)
    exports: list[ExportSpec] = []
    for position, raw_export in enumerate(_list(data.get("exports", []), "exports", locator)):
        export = _mapping(raw_export, f"exports[{position}]", locator)
        exports.append(
            ExportSpec(
                name=_string(export.get("name"), f"exports[{position}].name", locator),
                paths=_strings(export.get("paths"), f"exports[{position}].paths", locator),
                max_bytes=_integer(export.get("max_bytes", 20_000), f"exports[{position}].max_bytes", locator),
            )
        )
    export_paths = tuple(dict.fromkeys(path for export in exports for path in export.paths))
    raw_search = data.get("search")
    if raw_search is None:
        search = SearchSpec(paths=export_paths)
    else:
        search_data = _mapping(raw_search, "search", locator)
        search = SearchSpec(
            paths=_strings(search_data.get("paths"), "search.paths", locator),
            purpose=_string(search_data.get("purpose", "ranking-only"), "search.purpose", locator),
        )
    sensitivity = _one_of(
        _string(data.get("sensitivity", "private"), "sensitivity", locator),
        {"public", "private", "restricted", "local-only"},
        "sensitivity",
        locator,
    )
    sync_class = _one_of(
        _string(data.get("sync_class", "local-only"), "sync_class", locator),
        {"private-sync", "local-only"},
        "sync_class",
        locator,
    )
    intent_value = data.get("intent")
    intent = None if intent_value is None else _string(intent_value, "intent", locator)
    return VaultManifest(
        schema=schema,
        id=vault_id,
        name=_string(data.get("name"), "name", locator),
        purpose=_string(data.get("purpose", "Unspecified"), "purpose", locator),
        topics=_optional_strings(data.get("topics"), "topics", locator),
        sensitivity=sensitivity,
        sync_class=sync_class,
        default_branch=_string(data.get("default_branch", "main"), "default_branch", locator),
        intent=intent,
        record_roots=_optional_strings(data.get("record_roots"), "record_roots", locator),
        dependencies=_optional_strings(data.get("dependencies"), "dependencies", locator),
        search=search,
        exports=tuple(exports),
        root=root,
    )


@dataclass(frozen=True, slots=True)
class NodeEntry:
    id: str
    root: str
    manifest: str = "brain.yaml"


@dataclass(frozen=True, slots=True)
class NodeMap:
    schema: str
    node: str
    vaults: tuple[NodeEntry, ...]


def node_map_from_mapping(data: Mapping[str, Any], locator: str) -> NodeMap:
    schema = _string(data.get("schema"), "schema", locator)
    if schema != NODE_MAP_SCHEMA:
        _contract_error(locator, "schema", NODE_MAP_SCHEMA, schema)
    entries: list[NodeEntry] = []
    for position, raw_entry in enumerate(_list(data.get("vaults"), "vaults", locator)):
        entry = _mapping(raw_entry, f"vaults[{position}]", locator)
        entries.append(
            NodeEntry(
                id=_string(entry.get("id"), f"vaults[{position}].id", locator),
                root=_string(entry.get("root"), f"vaults[{position}].root", locator),
                manifest=_string(entry.get("manifest", "brain.yaml"), f"vaults[{position}].manifest", locator),
            )
        )
    return NodeMap(
        schema=schema,
        node=_string(data.get("node"), "node", locator),
        vaults=tuple(entries),
    )


@dataclass(frozen=True, slots=True)
class Grant:
    schema: str
    id: str
    issuer: str
    authenticated_source: str
    source_message_sha256: str
    session_id: str
    operation: str
    vault: str
    paths: tuple[str, ...]
    issued_at: datetime
    expires_at: datetime

    def allows(self, vault: str, path: str, now: datetime) -> bool:
        return (
            self.operation == "read"
            and self.vault == vault
            and path in self.paths
            and self.issued_at <= now < self.expires_at
        )

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["issued_at"] = self.issued_at.isoformat()
        result["expires_at"] = self.expires_at.isoformat()
        return result

    @property
    def sha256(self) -> str:
        return stable_hash(self.as_dict())


def grant_from_mapping(data: Mapping[str, Any], locator: str) -> Grant:
    schema = _string(data.get("schema"), "schema", locator)
    if schema != GRANT_SCHEMA:
        _contract_error(locator, "schema", GRANT_SCHEMA, schema)
    grant_id = _string(data.get("id"), "id", locator)
    if not _GRANT_ID.fullmatch(grant_id):
        _contract_error(locator, "id", "grant_<id>", grant_id)
    operation = _one_of(_string(data.get("operation"), "operation", locator), {"read"}, "operation", locator)
    source_hash = _string(data.get("source_message_sha256"), "source_message_sha256", locator)
    if not re.fullmatch(r"[0-9a-f]{64}", source_hash):
        _contract_error(locator, "source_message_sha256", "64 lowercase hexadecimal characters", source_hash)
    issued_at = _timestamp(data.get("issued_at"), "issued_at", locator)
    expires_at = _timestamp(data.get("expires_at"), "expires_at", locator)
    if expires_at <= issued_at:
        _contract_error(locator, "expires_at", "later than issued_at", data.get("expires_at"))
    return Grant(
        schema=schema,
        id=grant_id,
        issuer=_string(data.get("issuer"), "issuer", locator),
        authenticated_source=_string(data.get("authenticated_source"), "authenticated_source", locator),
        source_message_sha256=source_hash,
        session_id=_string(data.get("session_id"), "session_id", locator),
        operation=operation,
        vault=_string(data.get("vault"), "vault", locator),
        paths=_strings(data.get("paths"), "paths", locator),
        issued_at=issued_at,
        expires_at=expires_at,
    )


@dataclass(frozen=True, slots=True)
class Budget:
    source_tokens: int = 12_000
    vaults: int = 4
    excerpts: int = 12


@dataclass(frozen=True, slots=True)
class ContextRequest:
    request_id: str
    session_id: str
    intent: str
    named_vaults: tuple[str, ...] = ()
    requested_facets: tuple[str, ...] = ()
    mode: str = "think"
    node: str = "local"
    grants: tuple[Grant, ...] = ()
    budget: Budget = Budget()
    expansion_facet: str | None = None


@dataclass(frozen=True, slots=True)
class SourceReceipt:
    vault: str
    path: str
    content_sha256: str
    locator: str
    access: str
    source_tokens: int
    excerpt: str
    grant_sha256: str | None = None


@dataclass(frozen=True, slots=True)
class ExclusionReceipt:
    vault: str
    path: str | None
    reason: str
    code: str


@dataclass(frozen=True, slots=True)
class ContextPack:
    protocol_version: str
    request_id: str
    result: str
    selected_vaults: tuple[str, ...]
    considered_and_dropped: tuple[dict[str, str], ...]
    sources: tuple[SourceReceipt, ...]
    excluded: tuple[ExclusionReceipt, ...]
    warnings: tuple[str, ...]
    tokenizer: str
    policy_sha256: str
    pack_sha256: str
    pack_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
