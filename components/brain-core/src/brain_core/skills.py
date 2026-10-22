"""Deterministic, metadata-only skill discovery and routing.

The router deliberately knows nothing about models, providers, or networks.  A
catalog contains bounded frontmatter plus a locator for the instruction body.
The body is read only by :func:`load_selected_instructions`, after a unique
positive route and an explicit request from the caller.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from brain_core.contracts import parse_yaml
from brain_core.errors import BrainError, ErrorCode

SKILL_SCHEMA = "brain.skill/v1"
MAX_FRONTMATTER_BYTES = 16_384

# Scores are intentionally integers and public.  Exact trigger hits determine
# eligibility; token overlap only orders skills that already matched exactly.
EXPLICIT_INVOCATION_WEIGHT = 1_000_000
EXACT_TRIGGER_WEIGHT = 10_000
TRIGGER_TOKEN_WEIGHT = 100
DESCRIPTION_TOKEN_WEIGHT = 1

_SKILL_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_WORD = re.compile(r"[\w]+", flags=re.UNICODE)
_PORTABILITY = frozenset({"portable", "project", "host"})
_METADATA_FIELDS = frozenset(
    {
        "schema",
        "id",
        "version",
        "description",
        "positive_triggers",
        "negative_triggers",
        "triggers",
        "supported_modes",
        "required_authority",
        "dependencies",
        "host_capabilities",
        "portability",
        "owner",
    }
)


@dataclass(frozen=True, slots=True)
class SkillMetadata:
    """The only skill information visible before selection."""

    schema: str
    id: str
    version: str
    description: str
    positive_triggers: tuple[str, ...]
    negative_triggers: tuple[str, ...]
    supported_modes: tuple[str, ...]
    required_authority: tuple[str, ...]
    dependencies: tuple[str, ...]
    host_capabilities: tuple[str, ...]
    portability: str
    owner: str
    locator: str = field(default="", compare=False)


@dataclass(frozen=True, slots=True)
class SkillRouteRequest:
    """Runtime facts used by the local router."""

    intent: str
    mode: str
    explicit_skill_id: str | None = None
    authority: frozenset[str] = frozenset()
    available_dependencies: frozenset[str] = frozenset()
    host_capabilities: frozenset[str] = frozenset()
    project_owner: str | None = None


@dataclass(frozen=True, slots=True)
class SkillCandidate:
    """A plain routing result; it never contains instruction text."""

    id: str
    version: str
    description: str
    owner: str
    score: int
    matched_positive_triggers: tuple[str, ...]
    explicit: bool = False


@dataclass(frozen=True, slots=True)
class SkillExclusion:
    id: str
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SkillRoute:
    """One deterministic routing decision."""

    status: str
    selected: SkillCandidate | None = None
    candidates: tuple[SkillCandidate, ...] = ()
    unavailable: tuple[SkillExclusion, ...] = ()
    reason: str | None = None

    @property
    def is_unique_positive_selection(self) -> bool:
        return (
            self.status == "selected"
            and self.selected is not None
            and self.candidates == (self.selected,)
            and (self.selected.explicit or bool(self.selected.matched_positive_triggers))
        )


@dataclass(frozen=True, slots=True)
class _SkillSource:
    path: Path
    body_offset: int
    frontmatter_sha256: str


@dataclass(frozen=True, slots=True)
class SkillCatalog:
    """A stable metadata catalog with private, body-free source locators."""

    entries: tuple[SkillMetadata, ...]
    _sources: Mapping[str, _SkillSource] = field(
        default_factory=lambda: MappingProxyType({}), compare=False, repr=False
    )

    @classmethod
    def from_metadata(cls, entries: Iterable[SkillMetadata]) -> SkillCatalog:
        return cls(_validated_entries(entries))

    @classmethod
    def from_paths(
        cls,
        paths: Iterable[str | Path],
        *,
        max_frontmatter_bytes: int = MAX_FRONTMATTER_BYTES,
    ) -> SkillCatalog:
        metadata: list[SkillMetadata] = []
        sources: dict[str, _SkillSource] = {}
        for raw_path in paths:
            path = Path(raw_path)
            frontmatter, body_offset, digest = _read_frontmatter(path, max_frontmatter_bytes)
            item = parse_skill_metadata(frontmatter, str(path))
            metadata.append(item)
            sources[item.id] = _SkillSource(path.resolve(), body_offset, digest)
        entries = _validated_entries(metadata)
        return cls(entries, MappingProxyType(sources))

    def metadata(self, skill_id: str) -> SkillMetadata | None:
        return next((entry for entry in self.entries if entry.id == skill_id), None)


def parse_skill_metadata(frontmatter: str, locator: str = "<skill>") -> SkillMetadata:
    """Decode and validate one YAML frontmatter mapping."""

    data = parse_yaml(frontmatter, locator)
    unknown = sorted(set(data) - _METADATA_FIELDS)
    if unknown:
        _invalid(locator, "unknown fields", expected="known skill metadata fields", actual=unknown)

    schema = _required_string(data, "schema", locator)
    if schema != SKILL_SCHEMA:
        _invalid(locator, "schema", expected=SKILL_SCHEMA, actual=schema)

    skill_id = _required_string(data, "id", locator)
    if not _SKILL_ID.fullmatch(skill_id):
        _invalid(locator, "id", expected="lowercase kebab-case skill ID", actual=skill_id)

    description = _required_string(data, "description", locator)
    if "\n" in description or "\r" in description:
        _invalid(locator, "description", expected="one-line string", actual=description)

    flat_positive = data.get("positive_triggers")
    flat_negative = data.get("negative_triggers")
    nested = data.get("triggers")
    if nested is not None:
        if flat_positive is not None or flat_negative is not None:
            _invalid(
                locator,
                "triggers",
                expected="either nested or flat trigger fields, not both",
                actual="mixed trigger forms",
            )
        if not isinstance(nested, Mapping):
            _invalid(locator, "triggers", expected="mapping", actual=type(nested).__name__)
        unknown_triggers = sorted(set(nested) - {"positive", "negative"})
        if unknown_triggers:
            _invalid(locator, "triggers", expected="positive and negative", actual=unknown_triggers)
        flat_positive = nested.get("positive")
        flat_negative = nested.get("negative", [])

    positive = _string_list(flat_positive, "positive_triggers", locator, required=True)
    if flat_negative is None:
        flat_negative = []
    negative = _string_list(flat_negative, "negative_triggers", locator)
    modes = _string_list(data.get("supported_modes"), "supported_modes", locator, required=True)
    required_authority = _string_list(
        data.get("required_authority", []), "required_authority", locator
    )
    dependencies = _string_list(data.get("dependencies", []), "dependencies", locator)
    host_capabilities = _string_list(
        data.get("host_capabilities", []), "host_capabilities", locator
    )
    portability = _required_string(data, "portability", locator)
    if portability not in _PORTABILITY:
        _invalid(locator, "portability", expected=sorted(_PORTABILITY), actual=portability)

    return SkillMetadata(
        schema=schema,
        id=skill_id,
        version=_required_string(data, "version", locator),
        description=description,
        positive_triggers=positive,
        negative_triggers=negative,
        supported_modes=modes,
        required_authority=required_authority,
        dependencies=dependencies,
        host_capabilities=host_capabilities,
        portability=portability,
        owner=_required_string(data, "owner", locator),
        locator=locator,
    )


def read_skill_metadata(
    path: str | Path, *, max_frontmatter_bytes: int = MAX_FRONTMATTER_BYTES
) -> SkillMetadata:
    """Read only the bounded frontmatter of a ``SKILL.md`` file."""

    source = Path(path)
    frontmatter, _, _ = _read_frontmatter(source, max_frontmatter_bytes)
    return parse_skill_metadata(frontmatter, str(source))


def route_skill(catalog: SkillCatalog, request: SkillRouteRequest) -> SkillRoute:
    """Route a request using local string matching and declared capabilities."""

    explicit_id = request.explicit_skill_id
    if explicit_id is not None:
        metadata = catalog.metadata(explicit_id)
        if metadata is None:
            return SkillRoute(status="not_found", reason=f"Unknown skill: {explicit_id}")
        blockers = _availability_blockers(metadata, request)
        if blockers:
            return SkillRoute(
                status="unavailable",
                unavailable=(SkillExclusion(metadata.id, blockers),),
                reason=f"Explicit skill {metadata.id} is unavailable",
            )
        candidate = _candidate(metadata, request.intent, explicit=True)
        return SkillRoute(status="selected", selected=candidate, candidates=(candidate,))

    available: list[SkillCandidate] = []
    unavailable: list[SkillExclusion] = []
    for metadata in catalog.entries:
        negative_hits = _matching_triggers(request.intent, metadata.negative_triggers)
        if negative_hits:
            continue
        positive_hits = _matching_triggers(request.intent, metadata.positive_triggers)
        if not positive_hits:
            continue
        blockers = _availability_blockers(metadata, request)
        if blockers:
            unavailable.append(SkillExclusion(metadata.id, blockers))
            continue
        available.append(_candidate(metadata, request.intent, explicit=False))

    unavailable_result = tuple(sorted(unavailable, key=lambda item: item.id))
    if not available:
        if unavailable_result:
            return SkillRoute(
                status="unavailable",
                unavailable=unavailable_result,
                reason="Matching skills are unavailable in this runtime",
            )
        return SkillRoute(status="not_found", reason="No positive skill trigger matched")

    top_score = max(candidate.score for candidate in available)
    tied = tuple(sorted((item for item in available if item.score == top_score), key=lambda item: item.id))
    if len(tied) > 1:
        return SkillRoute(
            status="ambiguous",
            candidates=tied,
            unavailable=unavailable_result,
            reason="Top skill candidates are tied",
        )
    return SkillRoute(
        status="selected",
        selected=tied[0],
        candidates=tied,
        unavailable=unavailable_result,
    )


def load_selected_instructions(
    catalog: SkillCatalog,
    route: SkillRoute,
    *,
    requested: bool = False,
) -> str:
    """Load a selected skill body only after the caller explicitly requests it."""

    if not requested:
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            "Skill instruction loading requires an explicit request",
            details={"requested": False},
        )
    if not route.is_unique_positive_selection:
        raise BrainError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "Skill instructions require one unique positive selection",
            details={"route_status": route.status},
        )

    assert route.selected is not None  # narrowed by is_unique_positive_selection
    source = catalog._sources.get(route.selected.id)
    if source is None:
        raise BrainError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "Selected skill has no instruction source",
            details={"skill_id": route.selected.id},
        )
    try:
        with source.path.open("rb", buffering=0) as handle:
            frontmatter = handle.read(source.body_offset)
            if hashlib.sha256(frontmatter).hexdigest() != source.frontmatter_sha256:
                raise BrainError(
                    ErrorCode.SOURCE_CHANGED,
                    "Skill metadata changed after catalog construction",
                    details={"skill_id": route.selected.id, "locator": str(source.path)},
                )
            body = handle.read()
    except BrainError:
        raise
    except OSError as exc:
        raise BrainError(
            ErrorCode.SOURCE_UNREADABLE,
            "Unable to read selected skill instructions",
            details={"skill_id": route.selected.id, "locator": str(source.path)},
        ) from exc
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BrainError(
            ErrorCode.SOURCE_BINARY,
            "Selected skill instructions are not UTF-8 text",
            details={"skill_id": route.selected.id, "locator": str(source.path)},
        ) from exc


def _candidate(metadata: SkillMetadata, intent: str, *, explicit: bool) -> SkillCandidate:
    positive_hits = _matching_triggers(intent, metadata.positive_triggers)
    intent_tokens = set(_tokens(intent))
    trigger_tokens = set(
        token for trigger in metadata.positive_triggers for token in _tokens(trigger)
    )
    description_tokens = set(_tokens(metadata.description))
    score = (
        (EXPLICIT_INVOCATION_WEIGHT if explicit else 0)
        + EXACT_TRIGGER_WEIGHT * len(positive_hits)
        + TRIGGER_TOKEN_WEIGHT * len(intent_tokens & trigger_tokens)
        + DESCRIPTION_TOKEN_WEIGHT * len(intent_tokens & description_tokens)
    )
    return SkillCandidate(
        id=metadata.id,
        version=metadata.version,
        description=metadata.description,
        owner=metadata.owner,
        score=score,
        matched_positive_triggers=positive_hits,
        explicit=explicit,
    )


def _availability_blockers(
    metadata: SkillMetadata, request: SkillRouteRequest
) -> tuple[str, ...]:
    blockers: list[str] = []
    if request.mode not in metadata.supported_modes:
        blockers.append(f"unsupported-mode:{request.mode}")
    blockers.extend(
        f"missing-authority:{item}"
        for item in metadata.required_authority
        if item not in request.authority
    )
    blockers.extend(
        f"missing-dependency:{item}"
        for item in metadata.dependencies
        if item not in request.available_dependencies
    )
    blockers.extend(
        f"missing-host-capability:{item}"
        for item in metadata.host_capabilities
        if item not in request.host_capabilities
    )
    if metadata.portability == "project" and request.project_owner != metadata.owner:
        blockers.append(f"wrong-project-owner:{metadata.owner}")
    return tuple(blockers)


def _matching_triggers(intent: str, triggers: Sequence[str]) -> tuple[str, ...]:
    normalized_intent = f" {' '.join(_tokens(intent))} "
    return tuple(
        trigger
        for trigger in triggers
        if f" {' '.join(_tokens(trigger))} " in normalized_intent
    )


def _tokens(value: str) -> tuple[str, ...]:
    return tuple(match.group(0).casefold() for match in _WORD.finditer(value))


def _validated_entries(entries: Iterable[SkillMetadata]) -> tuple[SkillMetadata, ...]:
    ordered = sorted(entries, key=lambda item: item.id)
    seen: set[str] = set()
    for entry in ordered:
        if entry.id in seen:
            raise BrainError(
                ErrorCode.DUPLICATE_ID,
                f"Duplicate skill ID: {entry.id}",
                details={"skill_id": entry.id},
            )
        seen.add(entry.id)
    return tuple(ordered)


def _read_frontmatter(path: Path, max_bytes: int) -> tuple[str, int, str]:
    if max_bytes < 1:
        raise BrainError(
            ErrorCode.INVALID_ARGUMENT,
            "max_frontmatter_bytes must be positive",
            details={"max_frontmatter_bytes": max_bytes},
        )
    try:
        with path.open("rb", buffering=0) as handle:
            first = handle.readline(max_bytes + 1)
            if first.strip() != b"---":
                _invalid(str(path), "frontmatter", expected="opening --- delimiter", actual="missing")
            chunks: list[bytes] = []
            consumed = len(first)
            while consumed <= max_bytes:
                line = handle.readline(max_bytes - consumed + 1)
                if not line:
                    _invalid(
                        str(path),
                        "frontmatter",
                        expected="closing --- delimiter",
                        actual="end of file",
                    )
                consumed += len(line)
                if consumed > max_bytes:
                    break
                if line.strip() in {b"---", b"..."}:
                    prefix = first + b"".join(chunks) + line
                    try:
                        text = b"".join(chunks).decode("utf-8")
                    except UnicodeDecodeError as exc:
                        raise BrainError(
                            ErrorCode.CONTRACT_INVALID,
                            f"Skill frontmatter in {path} is not UTF-8",
                            details={"locator": str(path)},
                        ) from exc
                    return text, handle.tell(), hashlib.sha256(prefix).hexdigest()
                chunks.append(line)
    except BrainError:
        raise
    except OSError as exc:
        raise BrainError(
            ErrorCode.SOURCE_UNREADABLE,
            "Unable to read skill metadata",
            details={"locator": str(path)},
        ) from exc
    raise BrainError(
        ErrorCode.CONTRACT_INVALID,
        f"Skill frontmatter in {path} exceeds its byte limit",
        details={"locator": str(path), "max_bytes": max_bytes},
    )


def _required_string(data: Mapping[str, Any], field_name: str, locator: str) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value.strip():
        _invalid(locator, field_name, expected="non-empty string", actual=type(value).__name__)
    return value


def _string_list(
    value: Any,
    field_name: str,
    locator: str,
    *,
    required: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list):
        _invalid(locator, field_name, expected="list of non-empty strings", actual=type(value).__name__)
    result: list[str] = []
    seen: set[str] = set()
    for position, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            _invalid(
                locator,
                f"{field_name}[{position}]",
                expected="non-empty string",
                actual=type(item).__name__,
            )
        normalized = item.casefold()
        if normalized in seen:
            _invalid(locator, field_name, expected="unique values", actual=item)
        seen.add(normalized)
        result.append(item)
    if required and not result:
        _invalid(locator, field_name, expected="non-empty list", actual=[])
    return tuple(result)


def _invalid(locator: str, field_name: str, *, expected: Any, actual: Any) -> None:
    raise BrainError(
        ErrorCode.CONTRACT_INVALID,
        f"Invalid field {field_name} in {locator}",
        details={
            "locator": locator,
            "field": field_name,
            "expected": expected,
            "actual": actual,
        },
    )
