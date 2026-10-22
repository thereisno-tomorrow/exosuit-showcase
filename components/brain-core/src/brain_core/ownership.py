"""Pure, fail-closed ownership catalog parsing and resolution."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal, Mapping, TypeAlias

from ruamel.yaml import YAML
from ruamel.yaml.constructor import DuplicateKeyError
from ruamel.yaml.error import YAMLError

Sha256: TypeAlias = str
Locator: TypeAlias = str
ResponsibilityKey: TypeAlias = Literal[
    "system_architecture", "fleet_enrollment", "system_admin_policy",
    "migration_authority", "audit_authority", "recovery_planning",
    "system_decisions", "ciel_system_behavior", "ciel_conformance",
    "personal_memory", "cross_project_memory", "personal_preferences",
    "personal_commitments", "personal_continuity", "personal_ciel_preferences",
    "project_domain_truth", "project_decisions", "project_commitments",
    "project_intent", "project_evidence", "project_capabilities",
    "shared_vault_contracts", "shared_lifecycle_invariants", "ownership_enforcement",
    "permission_enforcement", "provenance_enforcement", "record_validation",
    "leased_content_access", "effect_safety", "bounded_resume",
    "receipt_validation", "ciel_interface",
]

_SCHEMA = "brain.ownership-catalog/v1"
_SHA = re.compile(r"[0-9a-f]{64}")
_RFC3339 = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_PROJECT_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,62}")
_PROJECT_PATTERN = "^[a-z0-9][a-z0-9-]{0,62}$"
_RESPONSIBILITIES = (
    "system_architecture", "fleet_enrollment", "system_admin_policy",
    "migration_authority", "audit_authority", "recovery_planning",
    "system_decisions", "ciel_system_behavior", "ciel_conformance",
    "personal_memory", "cross_project_memory", "personal_preferences",
    "personal_commitments", "personal_continuity", "personal_ciel_preferences",
    "project_domain_truth", "project_decisions", "project_commitments",
    "project_intent", "project_evidence", "project_capabilities",
    "shared_vault_contracts", "shared_lifecycle_invariants", "ownership_enforcement",
    "permission_enforcement", "provenance_enforcement", "record_validation",
    "leased_content_access", "effect_safety", "bounded_resume",
    "receipt_validation", "ciel_interface",
)
_RESPONSIBILITY_SET = frozenset(_RESPONSIBILITIES)
_TOP_FIELDS = frozenset({
    "schema", "catalog_id", "catalog_version", "status", "canonicalization",
    "system_authority_locator", "resolution_defaults", "responsibility_keys",
    "owners", "constraints", "refusal_codes", "source_anchors",
})
_OWNER_FIELDS = frozenset({
    "owner_id", "owner_kind", "locator", "parameter", "responsibilities", "active_writer",
})
_WRITER_FIELDS = frozenset({
    "state", "generation", "writer_locator", "valid_from", "expires_at",
    "required_preimage_sha256",
})
_PARAMETER_FIELDS = frozenset({"name", "pattern", "source"})
_OWNER_KINDS = frozenset({
    "canonical_vault", "parameterized_canonical_vault", "enforcement_component",
    "interface_contract",
})
_DEFAULTS: Mapping[str, object] = {
    "unknown": "refuse", "duplicate": "refuse", "ambiguous": "refuse",
    "zero_active_writers": "refuse", "multiple_active_writers": "refuse",
    "stale_generation": "refuse", "owner_mismatch": "refuse",
    "expired_generation": "refuse", "preimage_change": "refuse",
    "clock_source": "caller_supplied_utc", "comparison": "ordinal",
}
_CONSTRAINTS = frozenset({
    "exactly_one_owner_per_responsibility", "exactly_one_active_writer_generation",
    "interface_is_not_permission_authority", "interface_is_not_effect_executor",
    "enforcement_is_not_canonical_truth", "project_template_requires_exact_project_id",
    "writes_require_exact_owner_and_writer_generation", "writes_require_unexpired_generation",
    "writes_require_matching_preimage_when_bound", "historical_owner_records_are_not_current_authority",
})
_CODES: Mapping[str, str] = {
    "unknown_responsibility": "ownership_unknown",
    "duplicate_responsibility": "ownership_duplicate",
    "ambiguous_project": "ownership_ambiguous", "zero_active_writers": "writer_absent",
    "multiple_active_writers": "writer_multiple", "stale_generation": "writer_generation_stale",
    "owner_mismatch": "writer_owner_mismatch", "expired_generation": "writer_generation_expired",
    "preimage_change": "writer_preimage_changed",
}
_ANCHORS = frozenset({"governing_plan_sha256", "prep_inventory_sha256", "prep_packet_sha256"})


class OwnershipRefusalCode(StrEnum):
    UNKNOWN = "ownership_unknown"
    DUPLICATE = "ownership_duplicate"
    AMBIGUOUS = "ownership_ambiguous"
    WRITER_ABSENT = "writer_absent"
    WRITER_MULTIPLE = "writer_multiple"
    GENERATION_STALE = "writer_generation_stale"
    OWNER_MISMATCH = "writer_owner_mismatch"
    GENERATION_EXPIRED = "writer_generation_expired"
    PREIMAGE_CHANGED = "writer_preimage_changed"


class OwnershipCatalogError(ValueError):
    """Stable parse-time refusal for an invalid catalog document."""

    def __init__(self, code: str, detail: str, *, catalog_id: str | None = None) -> None:
        super().__init__(detail)
        self.code = code
        self.catalog_id = catalog_id
        self.detail = detail


def _value_object_error(detail: str) -> None:
    raise ValueError(detail)


def _valid_nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _valid_locator(value: object) -> bool:
    return _valid_nonempty(value) and value.startswith("vault://")


def _valid_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA.fullmatch(value) is not None


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None


@dataclass(frozen=True, slots=True)
class WriterGeneration:
    state: Literal["active"]
    generation: int
    writer_locator: Locator
    valid_from: datetime
    expires_at: datetime | None
    required_preimage_sha256: Sha256 | None

    def __post_init__(self) -> None:
        if self.state != "active":
            _value_object_error("state must be active")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool) or self.generation <= 0:
            _value_object_error("generation must be a positive integer")
        if not _valid_locator(self.writer_locator):
            _value_object_error("writer_locator must begin with vault://")
        if not _aware(self.valid_from):
            _value_object_error("valid_from must be a timezone-aware datetime")
        if self.expires_at is not None:
            if not _aware(self.expires_at):
                _value_object_error("expires_at must be a timezone-aware datetime or None")
            if self.expires_at <= self.valid_from:
                _value_object_error("expires_at must be later than valid_from")
        if self.required_preimage_sha256 is not None and not _valid_sha(self.required_preimage_sha256):
            _value_object_error("required_preimage_sha256 must be 64 lowercase hexadecimal characters or None")


@dataclass(frozen=True, slots=True)
class OwnershipEntry:
    owner_id: str
    owner_kind: str
    locator: Locator
    responsibilities: tuple[ResponsibilityKey, ...]
    active_writer: WriterGeneration
    project_parameter: str | None

    def __post_init__(self) -> None:
        if not _valid_nonempty(self.owner_id):
            _value_object_error("owner_id must be a non-empty string")
        if not isinstance(self.owner_kind, str) or self.owner_kind not in _OWNER_KINDS:
            _value_object_error("owner_kind is unknown")
        if not _valid_locator(self.locator):
            _value_object_error("locator must begin with vault://")
        if not isinstance(self.responsibilities, tuple) or not self.responsibilities:
            _value_object_error("responsibilities must be a non-empty tuple")
        if any(not isinstance(item, str) or item not in _RESPONSIBILITY_SET for item in self.responsibilities):
            _value_object_error("responsibilities contain an unknown key")
        if len(set(self.responsibilities)) != len(self.responsibilities):
            _value_object_error("responsibilities contain a duplicate key")
        if not isinstance(self.active_writer, WriterGeneration):
            _value_object_error("active_writer must be a WriterGeneration")
        if self.project_parameter not in (None, "project_id"):
            _value_object_error("project_parameter must be project_id or None")
        parameterized = self.owner_kind == "parameterized_canonical_vault"
        if parameterized != (self.project_parameter == "project_id"):
            _value_object_error("project_parameter and owner_kind disagree")
        if not _template_valid(self.locator, parameterized):
            _value_object_error("locator has an invalid project template")
        if not _template_valid(self.active_writer.writer_locator, parameterized):
            _value_object_error("writer_locator has an invalid project template")


@dataclass(frozen=True, slots=True)
class OwnershipCatalog:
    schema: Literal["brain.ownership-catalog/v1"]
    catalog_id: str
    catalog_version: int
    entries: tuple[OwnershipEntry, ...]
    source_sha256: Sha256

    def __post_init__(self) -> None:
        if self.schema != _SCHEMA:
            _value_object_error("schema is unknown")
        if not _valid_nonempty(self.catalog_id):
            _value_object_error("catalog_id must be a non-empty string")
        if not isinstance(self.catalog_version, int) or isinstance(self.catalog_version, bool) or self.catalog_version != 1:
            _value_object_error("catalog_version must be 1")
        if not isinstance(self.entries, tuple) or not self.entries:
            _value_object_error("entries must be a non-empty tuple")
        if any(not isinstance(entry, OwnershipEntry) for entry in self.entries):
            _value_object_error("entries must contain only OwnershipEntry values")
        owner_ids = tuple(entry.owner_id for entry in self.entries)
        if len(set(owner_ids)) != len(owner_ids):
            _value_object_error("entries contain a duplicate owner_id")
        claimed = tuple(item for entry in self.entries for item in entry.responsibilities)
        if len(set(claimed)) != len(claimed):
            _value_object_error("entries contain a duplicate responsibility")
        if frozenset(claimed) != _RESPONSIBILITY_SET:
            _value_object_error("entries do not own every responsibility")
        if not _valid_sha(self.source_sha256):
            _value_object_error("source_sha256 must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True, slots=True)
class OwnershipResolution:
    responsibility: ResponsibilityKey
    owner_id: str
    owner_locator: Locator
    writer_locator: Locator
    writer_generation: int
    catalog_id: str
    catalog_sha256: Sha256

    def __post_init__(self) -> None:
        if not isinstance(self.responsibility, str) or self.responsibility not in _RESPONSIBILITY_SET:
            _value_object_error("responsibility is unknown")
        if not _valid_nonempty(self.owner_id):
            _value_object_error("owner_id must be a non-empty string")
        for name, locator in (("owner_locator", self.owner_locator), ("writer_locator", self.writer_locator)):
            if not _valid_locator(locator) or "{" in locator or "}" in locator:
                _value_object_error(f"{name} must be a resolved vault:// locator")
        if not isinstance(self.writer_generation, int) or isinstance(self.writer_generation, bool) or self.writer_generation <= 0:
            _value_object_error("writer_generation must be a positive integer")
        if not _valid_nonempty(self.catalog_id):
            _value_object_error("catalog_id must be a non-empty string")
        if not _valid_sha(self.catalog_sha256):
            _value_object_error("catalog_sha256 must be 64 lowercase hexadecimal characters")


@dataclass(frozen=True, slots=True)
class OwnershipRefusal:
    responsibility: str
    code: OwnershipRefusalCode
    catalog_id: str | None
    detail: str

    def __post_init__(self) -> None:
        if not isinstance(self.responsibility, str):
            _value_object_error("responsibility must be a string")
        if not isinstance(self.code, OwnershipRefusalCode):
            _value_object_error("code must be an OwnershipRefusalCode")
        if self.catalog_id is not None and not _valid_nonempty(self.catalog_id):
            _value_object_error("catalog_id must be a non-empty string or None")
        if not _valid_nonempty(self.detail):
            _value_object_error("detail must be a non-empty string")


def _error(code: str, detail: str, catalog_id: str | None = None) -> None:
    raise OwnershipCatalogError(code, detail, catalog_id=catalog_id)


def _fields(data: Mapping[str, Any], allowed: frozenset[str], context: str, catalog_id: str | None) -> None:
    actual = frozenset(data)
    if actual != allowed:
        _error("ownership_catalog_invalid", f"{context} fields differ; unknown={sorted(actual - allowed)!r}; missing={sorted(allowed - actual)!r}", catalog_id)


def _mapping(value: object, context: str, catalog_id: str | None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        _error("ownership_catalog_invalid", f"{context} must be a mapping", catalog_id)
    return dict(value)


def _list(value: object, context: str, catalog_id: str | None) -> list[Any]:
    if not isinstance(value, list):
        _error("ownership_catalog_invalid", f"{context} must be a list", catalog_id)
    return value


def _string(value: object, context: str, catalog_id: str | None) -> str:
    if not isinstance(value, str) or not value:
        _error("ownership_catalog_invalid", f"{context} must be a non-empty string", catalog_id)
    return value


def _integer(value: object, context: str, catalog_id: str | None) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        _error("ownership_catalog_invalid", f"{context} must be a positive integer", catalog_id)
    return value


def _sha(value: object, context: str, catalog_id: str | None) -> str:
    raw = _string(value, context, catalog_id)
    if _SHA.fullmatch(raw) is None:
        _error("ownership_catalog_invalid", f"{context} must be 64 lowercase hexadecimal characters", catalog_id)
    return raw


def _locator(value: object, context: str, catalog_id: str | None) -> str:
    raw = _string(value, context, catalog_id)
    if not raw.startswith("vault://"):
        _error("ownership_catalog_invalid", f"{context} must begin with vault://", catalog_id)
    return raw


def _timestamp(value: object, context: str, catalog_id: str | None) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = _string(value, context, catalog_id)
        if _RFC3339.fullmatch(raw) is None:
            _error("ownership_catalog_invalid", f"{context} must be an RFC 3339 timestamp", catalog_id)
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            _error("ownership_catalog_invalid", f"{context} must be an RFC 3339 timestamp", catalog_id)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _error("ownership_catalog_invalid", f"{context} must include a UTC offset", catalog_id)
    return parsed


def _writer(value: object, context: str, catalog_id: str) -> WriterGeneration:
    if value is None or value == []:
        _error(OwnershipRefusalCode.WRITER_ABSENT.value, f"{context} has no active writer", catalog_id)
    if isinstance(value, list):
        if len(value) != 1:
            _error(OwnershipRefusalCode.WRITER_MULTIPLE.value, f"{context} has multiple active writers", catalog_id)
        value = value[0]
    data = _mapping(value, f"{context}.active_writer", catalog_id)
    _fields(data, _WRITER_FIELDS, f"{context}.active_writer", catalog_id)
    if data["state"] != "active":
        _error(OwnershipRefusalCode.WRITER_ABSENT.value, f"{context} has no active writer", catalog_id)
    generation = _integer(data["generation"], f"{context}.active_writer.generation", catalog_id)
    locator = _locator(data["writer_locator"], f"{context}.active_writer.writer_locator", catalog_id)
    valid_from = _timestamp(data["valid_from"], f"{context}.active_writer.valid_from", catalog_id)
    expires_at = None if data["expires_at"] is None else _timestamp(data["expires_at"], f"{context}.active_writer.expires_at", catalog_id)
    if expires_at is not None and expires_at <= valid_from:
        _error("ownership_catalog_invalid", f"{context}.active_writer.expires_at must be later than valid_from", catalog_id)
    raw_preimage = data["required_preimage_sha256"]
    preimage = None if raw_preimage is None else _sha(raw_preimage, f"{context}.active_writer.required_preimage_sha256", catalog_id)
    return WriterGeneration("active", generation, locator, valid_from, expires_at, preimage)


def _parameter(value: object, context: str, catalog_id: str) -> str:
    data = _mapping(value, f"{context}.parameter", catalog_id)
    _fields(data, _PARAMETER_FIELDS, f"{context}.parameter", catalog_id)
    name = _string(data["name"], f"{context}.parameter.name", catalog_id)
    if name != "project_id" or data["pattern"] != _PROJECT_PATTERN or data["source"] != "explicit_request_scope":
        _error("ownership_catalog_invalid", f"{context}.parameter is not the frozen project parameter", catalog_id)
    return name


def _template_valid(locator: str, parameterized: bool) -> bool:
    has_project = "{project_id}" in locator
    has_any = "{" in locator or "}" in locator
    return parameterized == has_project and (has_project or not has_any)


def _entry(value: object, position: int, catalog_id: str) -> OwnershipEntry:
    context = f"owners[{position}]"
    data = _mapping(value, context, catalog_id)
    allowed = _OWNER_FIELDS if "parameter" in data else _OWNER_FIELDS - {"parameter"}
    _fields(data, allowed, context, catalog_id)
    owner_id = _string(data["owner_id"], f"{context}.owner_id", catalog_id)
    owner_kind = _string(data["owner_kind"], f"{context}.owner_kind", catalog_id)
    if owner_kind not in _OWNER_KINDS:
        _error("ownership_catalog_invalid", f"{context}.owner_kind is unknown", catalog_id)
    locator = _locator(data["locator"], f"{context}.locator", catalog_id)
    parameter = _parameter(data["parameter"], context, catalog_id) if "parameter" in data else None
    parameterized = owner_kind == "parameterized_canonical_vault"
    if parameterized != (parameter is not None) or not _template_valid(locator, parameterized):
        _error("ownership_catalog_invalid", f"{context} has an invalid project parameter or locator", catalog_id)
    raw_responsibilities = _list(data["responsibilities"], f"{context}.responsibilities", catalog_id)
    if not raw_responsibilities:
        _error("ownership_catalog_invalid", f"{context}.responsibilities must not be empty", catalog_id)
    responsibilities: list[ResponsibilityKey] = []
    for item in raw_responsibilities:
        responsibility = _string(item, f"{context}.responsibilities[]", catalog_id)
        if responsibility not in _RESPONSIBILITY_SET:
            _error(OwnershipRefusalCode.UNKNOWN.value, f"unknown responsibility: {responsibility}", catalog_id)
        if responsibility in responsibilities:
            _error(OwnershipRefusalCode.DUPLICATE.value, f"duplicate responsibility: {responsibility}", catalog_id)
        responsibilities.append(responsibility)  # type: ignore[arg-type]
    active_writer = _writer(data["active_writer"], context, catalog_id)
    if not _template_valid(active_writer.writer_locator, parameterized):
        _error("ownership_catalog_invalid", f"{context}.active_writer.writer_locator has an invalid project template", catalog_id)
    return OwnershipEntry(owner_id, owner_kind, locator, tuple(responsibilities), active_writer, parameter)


def parse_ownership_catalog(document: str | bytes, *, expected_sha256: Sha256) -> OwnershipCatalog:
    """Parse exact supplied bytes into an immutable ownership catalog."""

    _sha(expected_sha256, "expected_sha256", None)
    if isinstance(document, str):
        raw, text = document.encode("utf-8"), document
    elif isinstance(document, bytes):
        raw = document
        try:
            text = document.decode("utf-8")
        except UnicodeDecodeError:
            _error("ownership_catalog_invalid", "document must be valid UTF-8")
    else:
        _error("ownership_catalog_invalid", "document must be str or bytes")
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        _error("ownership_catalog_hash_mismatch", f"catalog hash mismatch: expected {expected_sha256}; actual {actual_sha256}")
    yaml = YAML(typ="safe", pure=True)
    yaml.allow_duplicate_keys = False
    try:
        loaded = yaml.load(text)
    except DuplicateKeyError:
        _error("duplicate_key", "catalog contains a duplicate mapping key")
    except YAMLError:
        _error("ownership_catalog_invalid", "catalog is not valid YAML")
    data = _mapping(loaded, "catalog", None)
    candidate = data.get("catalog_id")
    catalog_id = candidate if isinstance(candidate, str) and candidate else None
    _fields(data, _TOP_FIELDS, "catalog", catalog_id)
    catalog_id = _string(data["catalog_id"], "catalog.catalog_id", catalog_id)
    if data["schema"] != _SCHEMA:
        _error("ownership_catalog_invalid", "catalog.schema is unknown", catalog_id)
    catalog_version = _integer(data["catalog_version"], "catalog.catalog_version", catalog_id)
    if catalog_version != 1:
        _error("ownership_catalog_invalid", "catalog.catalog_version is unknown", catalog_id)
    if data["status"] != "active":
        _error("ownership_catalog_invalid", "catalog.status is not active", catalog_id)
    if data["canonicalization"] != "utf8-lf-yaml-key-order-as-written":
        _error("ownership_catalog_invalid", "catalog.canonicalization is unknown", catalog_id)
    _locator(data["system_authority_locator"], "catalog.system_authority_locator", catalog_id)
    if _mapping(data["resolution_defaults"], "catalog.resolution_defaults", catalog_id) != _DEFAULTS:
        _error("ownership_catalog_invalid", "catalog.resolution_defaults differ from v1", catalog_id)
    constraints = _mapping(data["constraints"], "catalog.constraints", catalog_id)
    if frozenset(constraints) != _CONSTRAINTS or any(value is not True for value in constraints.values()):
        _error("ownership_catalog_invalid", "catalog.constraints differ from v1", catalog_id)
    if _mapping(data["refusal_codes"], "catalog.refusal_codes", catalog_id) != _CODES:
        _error("ownership_catalog_invalid", "catalog.refusal_codes differ from v1", catalog_id)
    anchors = _mapping(data["source_anchors"], "catalog.source_anchors", catalog_id)
    _fields(anchors, _ANCHORS, "catalog.source_anchors", catalog_id)
    for key, value in anchors.items():
        _sha(value, f"catalog.source_anchors.{key}", catalog_id)
    raw_keys = _list(data["responsibility_keys"], "catalog.responsibility_keys", catalog_id)
    keys: list[str] = []
    for item in raw_keys:
        key = _string(item, "catalog.responsibility_keys[]", catalog_id)
        if key not in _RESPONSIBILITY_SET:
            _error(OwnershipRefusalCode.UNKNOWN.value, f"unknown responsibility: {key}", catalog_id)
        if key in keys:
            _error(OwnershipRefusalCode.DUPLICATE.value, f"duplicate responsibility: {key}", catalog_id)
        keys.append(key)
    if frozenset(keys) != _RESPONSIBILITY_SET:
        _error(OwnershipRefusalCode.UNKNOWN.value, f"missing responsibilities: {sorted(_RESPONSIBILITY_SET - frozenset(keys))!r}", catalog_id)
    entries = tuple(_entry(value, position, catalog_id) for position, value in enumerate(_list(data["owners"], "catalog.owners", catalog_id)))
    owner_ids: set[str] = set()
    claimed: set[str] = set()
    for entry in entries:
        if entry.owner_id in owner_ids:
            _error("ownership_catalog_invalid", f"duplicate owner_id: {entry.owner_id}", catalog_id)
        owner_ids.add(entry.owner_id)
        for responsibility in entry.responsibilities:
            if responsibility in claimed:
                _error(OwnershipRefusalCode.DUPLICATE.value, f"duplicate responsibility: {responsibility}", catalog_id)
            claimed.add(responsibility)
    if claimed != _RESPONSIBILITY_SET:
        _error(OwnershipRefusalCode.UNKNOWN.value, f"unowned responsibilities: {sorted(_RESPONSIBILITY_SET - claimed)!r}", catalog_id)
    return OwnershipCatalog(_SCHEMA, catalog_id, catalog_version, entries, actual_sha256)


def _refusal(catalog: OwnershipCatalog, responsibility: str, code: OwnershipRefusalCode, detail: str) -> OwnershipRefusal:
    return OwnershipRefusal(responsibility, code, catalog.catalog_id, detail)


def resolve_ownership(
    catalog: OwnershipCatalog,
    responsibility: str,
    *,
    project_id: str | None,
    at: datetime,
    expected_owner_locator: Locator | None = None,
    expected_writer_generation: int | None = None,
    observed_preimage_sha256: Sha256 | None = None,
) -> OwnershipResolution | OwnershipRefusal:
    """Resolve one responsibility without consulting mutable ambient state."""

    if not isinstance(responsibility, str) or responsibility not in _RESPONSIBILITY_SET:
        rendered = responsibility if isinstance(responsibility, str) else repr(responsibility)
        return _refusal(catalog, rendered, OwnershipRefusalCode.UNKNOWN, "responsibility is unknown")
    matches = tuple(entry for entry in catalog.entries if responsibility in entry.responsibilities)
    if not matches:
        return _refusal(catalog, responsibility, OwnershipRefusalCode.UNKNOWN, "responsibility is unowned")
    if len(matches) != 1:
        return _refusal(catalog, responsibility, OwnershipRefusalCode.DUPLICATE, "responsibility has multiple owners")
    entry = matches[0]
    if entry.project_parameter is not None and (not isinstance(project_id, str) or _PROJECT_ID.fullmatch(project_id) is None):
        return _refusal(catalog, responsibility, OwnershipRefusalCode.AMBIGUOUS, "an exact project_id is required")
    if not isinstance(at, datetime) or at.tzinfo is None or at.utcoffset() is None:
        raise ValueError("at must be a timezone-aware datetime")
    if expected_owner_locator is not None and not isinstance(expected_owner_locator, str):
        raise ValueError("expected_owner_locator must be a string or None")
    if expected_writer_generation is not None and (not isinstance(expected_writer_generation, int) or isinstance(expected_writer_generation, bool) or expected_writer_generation <= 0):
        raise ValueError("expected_writer_generation must be a positive integer or None")
    if observed_preimage_sha256 is not None and (not isinstance(observed_preimage_sha256, str) or _SHA.fullmatch(observed_preimage_sha256) is None):
        raise ValueError("observed_preimage_sha256 must be 64 lowercase hexadecimal characters or None")
    owner_locator = entry.locator.replace("{project_id}", project_id or "")
    writer_locator = entry.active_writer.writer_locator.replace("{project_id}", project_id or "")
    if expected_writer_generation is not None and expected_writer_generation != entry.active_writer.generation:
        return _refusal(catalog, responsibility, OwnershipRefusalCode.GENERATION_STALE, "writer generation does not match the expected generation")
    if expected_owner_locator is not None and expected_owner_locator != owner_locator:
        return _refusal(catalog, responsibility, OwnershipRefusalCode.OWNER_MISMATCH, "resolved owner does not match the expected owner")
    if at < entry.active_writer.valid_from:
        return _refusal(catalog, responsibility, OwnershipRefusalCode.GENERATION_STALE, "writer generation is not yet valid")
    if entry.active_writer.expires_at is not None and at >= entry.active_writer.expires_at:
        return _refusal(catalog, responsibility, OwnershipRefusalCode.GENERATION_EXPIRED, "writer generation has expired")
    required = entry.active_writer.required_preimage_sha256
    if required is not None and observed_preimage_sha256 != required:
        return _refusal(catalog, responsibility, OwnershipRefusalCode.PREIMAGE_CHANGED, "observed preimage does not match the bound preimage")
    return OwnershipResolution(
        responsibility=responsibility,  # type: ignore[arg-type]
        owner_id=entry.owner_id,
        owner_locator=owner_locator,
        writer_locator=writer_locator,
        writer_generation=entry.active_writer.generation,
        catalog_id=catalog.catalog_id,
        catalog_sha256=catalog.source_sha256,
    )
