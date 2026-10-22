from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import brain_core.ownership as ownership
from brain_core.ownership import (
    OwnershipCatalog,
    OwnershipCatalogError,
    OwnershipEntry,
    OwnershipRefusal,
    OwnershipRefusalCode,
    OwnershipResolution,
    WriterGeneration,
    parse_ownership_catalog,
    resolve_ownership,
)


NOW = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
PROJECT_KEYS = tuple(key for key in ownership._RESPONSIBILITIES if key.startswith("project_"))


def valid_payload() -> dict[str, Any]:
    ordinary = tuple(key for key in ownership._RESPONSIBILITIES if key not in PROJECT_KEYS)
    writer = {
        "state": "active",
        "generation": 1,
        "writer_locator": "vault://meta-vault",
        "valid_from": "2026-08-29T00:00:00+08:00",
        "expires_at": None,
        "required_preimage_sha256": None,
    }
    return {
        "schema": "brain.ownership-catalog/v1",
        "catalog_id": "fixture-catalog",
        "catalog_version": 1,
        "status": "active",
        "canonicalization": "utf8-lf-yaml-key-order-as-written",
        "system_authority_locator": "vault://meta-vault",
        "resolution_defaults": dict(ownership._DEFAULTS),
        "responsibility_keys": list(ownership._RESPONSIBILITIES),
        "owners": [
            {
                "owner_id": "metavault",
                "owner_kind": "canonical_vault",
                "locator": "vault://meta-vault",
                "responsibilities": list(ordinary),
                "active_writer": writer,
            },
            {
                "owner_id": "project-vault",
                "owner_kind": "parameterized_canonical_vault",
                "locator": "vault://project/{project_id}",
                "parameter": {
                    "name": "project_id",
                    "pattern": "^[a-z0-9][a-z0-9-]{0,62}$",
                    "source": "explicit_request_scope",
                },
                "responsibilities": list(PROJECT_KEYS),
                "active_writer": {
                    **writer,
                    "writer_locator": "vault://project/{project_id}",
                },
            },
        ],
        "constraints": {key: True for key in ownership._CONSTRAINTS},
        "refusal_codes": dict(ownership._CODES),
        "source_anchors": {key: "a" * 64 for key in ownership._ANCHORS},
    }


def rendered(payload: object) -> tuple[str, str]:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text, hashlib.sha256(text.encode()).hexdigest()


def parse_payload(payload: object | None = None) -> OwnershipCatalog:
    text, digest = rendered(valid_payload() if payload is None else payload)
    return parse_ownership_catalog(text, expected_sha256=digest)


def parse_error(payload: object, code: str = "ownership_catalog_invalid") -> OwnershipCatalogError:
    text, digest = rendered(payload)
    with pytest.raises(OwnershipCatalogError) as caught:
        parse_ownership_catalog(text, expected_sha256=digest)
    assert caught.value.code == code
    return caught.value


def changed(path: tuple[object, ...], value: object) -> dict[str, Any]:
    payload = valid_payload()
    target: Any = payload
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    return payload


def unsafe_catalog(catalog: OwnershipCatalog, entries: tuple[OwnershipEntry, ...]) -> OwnershipCatalog:
    """Build a corruption fixture without weakening public constructors."""
    corrupted = object.__new__(OwnershipCatalog)
    for name, value in (
        ("schema", catalog.schema),
        ("catalog_id", catalog.catalog_id),
        ("catalog_version", catalog.catalog_version),
        ("entries", entries),
        ("source_sha256", catalog.source_sha256),
    ):
        object.__setattr__(corrupted, name, value)
    return corrupted


def test_parse_valid_text_and_bytes_into_frozen_deterministic_values() -> None:
    payload = valid_payload()
    text, digest = rendered(payload)
    text_catalog = parse_ownership_catalog(text, expected_sha256=digest)
    byte_catalog = parse_ownership_catalog(text.encode(), expected_sha256=digest)
    assert text_catalog == byte_catalog
    assert text_catalog.schema == "brain.ownership-catalog/v1"
    assert text_catalog.catalog_id == "fixture-catalog"
    assert text_catalog.catalog_version == 1
    assert text_catalog.source_sha256 == digest
    assert tuple(entry.owner_id for entry in text_catalog.entries) == ("metavault", "project-vault")
    assert text_catalog.entries[1].project_parameter == "project_id"
    assert text_catalog.entries[0].active_writer.valid_from.utcoffset() == timedelta(hours=8)
    with pytest.raises(FrozenInstanceError):
        text_catalog.catalog_id = "changed"  # type: ignore[misc]


def test_resolution_returns_exact_owner_and_project_substitution() -> None:
    catalog = parse_payload()
    ordinary = resolve_ownership(catalog, "system_decisions", project_id=None, at=NOW)
    assert ordinary == OwnershipResolution(
        responsibility="system_decisions",
        owner_id="metavault",
        owner_locator="vault://meta-vault",
        writer_locator="vault://meta-vault",
        writer_generation=1,
        catalog_id="fixture-catalog",
        catalog_sha256=catalog.source_sha256,
    )
    project = resolve_ownership(catalog, "project_domain_truth", project_id="meta-vault", at=NOW)
    assert isinstance(project, OwnershipResolution)
    assert project.owner_locator == project.writer_locator == "vault://project/meta-vault"


@pytest.mark.parametrize("project_id", [None, "", "Upper", "-leading", "x" * 64, 1])
def test_resolution_refuses_ambiguous_project(project_id: object) -> None:
    result = resolve_ownership(
        parse_payload(), "project_domain_truth", project_id=project_id, at=NOW  # type: ignore[arg-type]
    )
    assert isinstance(result, OwnershipRefusal)
    assert result.code is OwnershipRefusalCode.AMBIGUOUS
    assert result.catalog_id == "fixture-catalog"


def test_resolution_refuses_unknown_unowned_and_duplicate_responsibilities() -> None:
    catalog = parse_payload()
    for responsibility in ("unknown", "", 1):
        result = resolve_ownership(catalog, responsibility, project_id=None, at=NOW)  # type: ignore[arg-type]
        assert isinstance(result, OwnershipRefusal)
        assert result.code is OwnershipRefusalCode.UNKNOWN
        assert result.responsibility == (responsibility if isinstance(responsibility, str) else repr(responsibility))
    first = catalog.entries[0]
    without = replace(first, responsibilities=tuple(key for key in first.responsibilities if key != "system_decisions"))
    unowned = unsafe_catalog(catalog, (without, catalog.entries[1]))
    assert resolve_ownership(unowned, "system_decisions", project_id=None, at=NOW).code is OwnershipRefusalCode.UNKNOWN  # type: ignore[union-attr]
    duplicate = unsafe_catalog(catalog, (first, first, catalog.entries[1]))
    assert resolve_ownership(duplicate, "system_decisions", project_id=None, at=NOW).code is OwnershipRefusalCode.DUPLICATE  # type: ignore[union-attr]


def test_resolution_refuses_generation_owner_time_and_preimage_claims() -> None:
    catalog = parse_payload()
    kwargs = {"catalog": catalog, "responsibility": "system_decisions", "project_id": None, "at": NOW}
    stale = resolve_ownership(**kwargs, expected_writer_generation=2)
    mismatch = resolve_ownership(**kwargs, expected_owner_locator="vault://brain-home")
    before = resolve_ownership(**{**kwargs, "at": datetime(2026, 8, 28, tzinfo=timezone.utc)})
    assert stale.code is OwnershipRefusalCode.GENERATION_STALE  # type: ignore[union-attr]
    assert mismatch.code is OwnershipRefusalCode.OWNER_MISMATCH  # type: ignore[union-attr]
    assert before.code is OwnershipRefusalCode.GENERATION_STALE  # type: ignore[union-attr]

    first = catalog.entries[0]
    expiry = NOW + timedelta(hours=1)
    expiring = replace(first, active_writer=replace(first.active_writer, expires_at=expiry))
    expiring_catalog = replace(catalog, entries=(expiring, catalog.entries[1]))
    expired = resolve_ownership(expiring_catalog, "system_decisions", project_id=None, at=expiry)
    assert expired.code is OwnershipRefusalCode.GENERATION_EXPIRED  # type: ignore[union-attr]

    required = "b" * 64
    bound = replace(first, active_writer=replace(first.active_writer, required_preimage_sha256=required))
    bound_catalog = replace(catalog, entries=(bound, catalog.entries[1]))
    for observed in (None, "c" * 64):
        refusal = resolve_ownership(
            bound_catalog, "system_decisions", project_id=None, at=NOW,
            observed_preimage_sha256=observed,
        )
        assert refusal.code is OwnershipRefusalCode.PREIMAGE_CHANGED  # type: ignore[union-attr]
    assert isinstance(resolve_ownership(
        bound_catalog, "system_decisions", project_id=None, at=NOW,
        observed_preimage_sha256=required,
    ), OwnershipResolution)


@pytest.mark.parametrize("at", ["not-time", datetime(2026, 8, 29)])
def test_resolution_rejects_invalid_caller_time(at: object) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_ownership(parse_payload(), "system_decisions", project_id=None, at=at)  # type: ignore[arg-type]


def test_resolution_rejects_invalid_optional_claim_types() -> None:
    catalog = parse_payload()
    with pytest.raises(ValueError, match="expected_owner_locator"):
        resolve_ownership(catalog, "system_decisions", project_id=None, at=NOW, expected_owner_locator=1)  # type: ignore[arg-type]
    for generation in (True, 0, "1"):
        with pytest.raises(ValueError, match="expected_writer_generation"):
            resolve_ownership(catalog, "system_decisions", project_id=None, at=NOW, expected_writer_generation=generation)  # type: ignore[arg-type]
    for preimage in (1, "A" * 64, "a" * 63):
        with pytest.raises(ValueError, match="observed_preimage_sha256"):
            resolve_ownership(catalog, "system_decisions", project_id=None, at=NOW, observed_preimage_sha256=preimage)  # type: ignore[arg-type]


def test_document_hash_utf8_yaml_and_mapping_failures_are_closed() -> None:
    text, digest = rendered(valid_payload())
    with pytest.raises(OwnershipCatalogError) as caught:
        parse_ownership_catalog(text, expected_sha256="0" * 64)
    assert caught.value.code == "ownership_catalog_hash_mismatch"
    assert caught.value.catalog_id is None
    for expected in ("A" * 64, "a" * 63, 1):
        with pytest.raises(OwnershipCatalogError) as caught:
            parse_ownership_catalog(text, expected_sha256=expected)  # type: ignore[arg-type]
        assert caught.value.code == "ownership_catalog_invalid"
    with pytest.raises(OwnershipCatalogError, match="valid UTF-8"):
        parse_ownership_catalog(b"\xff", expected_sha256=hashlib.sha256(b"\xff").hexdigest())
    with pytest.raises(OwnershipCatalogError, match="str or bytes"):
        parse_ownership_catalog(1, expected_sha256=digest)  # type: ignore[arg-type]
    for raw, code in (("[", "ownership_catalog_invalid"), ("[]", "ownership_catalog_invalid"), ("a: 1\na: 2\n", "duplicate_key")):
        with pytest.raises(OwnershipCatalogError) as caught:
            parse_ownership_catalog(raw, expected_sha256=hashlib.sha256(raw.encode()).hexdigest())
        assert caught.value.code == code


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema",), "brain.ownership-catalog/v2"),
        (("catalog_id",), ""),
        (("catalog_version",), True),
        (("catalog_version",), 2),
        (("status",), "inactive"),
        (("canonicalization",), "other"),
        (("system_authority_locator",), "file://bad"),
        (("resolution_defaults",), []),
        (("resolution_defaults", "comparison"), "casefold"),
        (("constraints",), []),
        (("constraints", "exactly_one_owner_per_responsibility"), False),
        (("refusal_codes",), []),
        (("refusal_codes", "unknown_responsibility"), "wrong"),
        (("source_anchors",), []),
        (("source_anchors", "governing_plan_sha256"), "bad"),
        (("responsibility_keys",), "system_decisions"),
        (("responsibility_keys", 0), 1),
        (("owners",), "owner"),
    ],
)
def test_catalog_top_level_fields_are_strict(path: tuple[object, ...], value: object) -> None:
    parse_error(changed(path, value))


def test_catalog_rejects_unknown_and_missing_top_or_anchor_fields() -> None:
    payload = valid_payload()
    payload["unknown"] = True
    parse_error(payload)
    payload = valid_payload()
    del payload["status"]
    parse_error(payload)
    payload = valid_payload()
    payload["source_anchors"]["unknown"] = "a" * 64
    parse_error(payload)
    payload = valid_payload()
    del payload["source_anchors"]["prep_packet_sha256"]
    parse_error(payload)


def test_catalog_responsibility_inventory_rejects_unknown_duplicate_missing_and_unowned() -> None:
    payload = valid_payload()
    payload["responsibility_keys"][0] = "unknown"
    parse_error(payload, OwnershipRefusalCode.UNKNOWN.value)
    payload = valid_payload()
    payload["responsibility_keys"][1] = payload["responsibility_keys"][0]
    parse_error(payload, OwnershipRefusalCode.DUPLICATE.value)
    payload = valid_payload()
    payload["responsibility_keys"].pop()
    parse_error(payload, OwnershipRefusalCode.UNKNOWN.value)
    payload = valid_payload()
    payload["owners"][0]["responsibilities"].remove("system_decisions")
    parse_error(payload, OwnershipRefusalCode.UNKNOWN.value)
    payload = valid_payload()
    payload["owners"][0]["responsibilities"].append("project_domain_truth")
    parse_error(payload, OwnershipRefusalCode.DUPLICATE.value)
    payload = valid_payload()
    payload["owners"][0]["responsibilities"].append("system_decisions")
    parse_error(payload, OwnershipRefusalCode.DUPLICATE.value)


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        (("owners", 0), "owner", "ownership_catalog_invalid"),
        (("owners", 0, "owner_id"), "", "ownership_catalog_invalid"),
        (("owners", 0, "owner_kind"), "unknown", "ownership_catalog_invalid"),
        (("owners", 0, "locator"), "file://bad", "ownership_catalog_invalid"),
        (("owners", 0, "responsibilities"), "system_decisions", "ownership_catalog_invalid"),
        (("owners", 0, "responsibilities"), [], "ownership_catalog_invalid"),
        (("owners", 0, "responsibilities", 0), 1, "ownership_catalog_invalid"),
        (("owners", 0, "responsibilities", 0), "unknown", "ownership_unknown"),
        (("owners", 0, "active_writer"), None, "writer_absent"),
        (("owners", 0, "active_writer"), [], "writer_absent"),
        (("owners", 0, "active_writer", "state"), "inactive", "writer_absent"),
        (("owners", 0, "active_writer", "generation"), True, "ownership_catalog_invalid"),
        (("owners", 0, "active_writer", "generation"), 0, "ownership_catalog_invalid"),
        (("owners", 0, "active_writer", "writer_locator"), "file://bad", "ownership_catalog_invalid"),
        (("owners", 0, "active_writer", "valid_from"), "not-time", "ownership_catalog_invalid"),
        (("owners", 0, "active_writer", "valid_from"), "2026-99-29T00:00:00Z", "ownership_catalog_invalid"),
        (("owners", 0, "active_writer", "valid_from"), "2026-08-29T00:00:00", "ownership_catalog_invalid"),
        (("owners", 0, "active_writer", "expires_at"), "2026-08-28T00:00:00Z", "ownership_catalog_invalid"),
        (("owners", 0, "active_writer", "required_preimage_sha256"), "bad", "ownership_catalog_invalid"),
    ],
)
def test_owner_and_writer_fields_are_strict(path: tuple[object, ...], value: object, code: str) -> None:
    parse_error(changed(path, value), code)


def test_owner_writer_shapes_duplicates_and_project_templates_are_strict() -> None:
    payload = valid_payload()
    payload["owners"][0]["unknown"] = True
    parse_error(payload)
    payload = valid_payload()
    del payload["owners"][0]["owner_id"]
    parse_error(payload)
    payload = valid_payload()
    payload["owners"][0]["active_writer"] = [copy.deepcopy(payload["owners"][0]["active_writer"])]
    assert parse_payload(payload).entries[0].active_writer.generation == 1
    payload = valid_payload()
    writer = copy.deepcopy(payload["owners"][0]["active_writer"])
    payload["owners"][0]["active_writer"] = [writer, copy.deepcopy(writer)]
    parse_error(payload, OwnershipRefusalCode.WRITER_MULTIPLE.value)
    payload = valid_payload()
    payload["owners"][0]["active_writer"] = "writer"
    parse_error(payload)
    payload = valid_payload()
    payload["owners"][0]["active_writer"]["unknown"] = True
    parse_error(payload)
    payload = valid_payload()
    del payload["owners"][0]["active_writer"]["state"]
    parse_error(payload)
    payload = valid_payload()
    payload["owners"].append(copy.deepcopy(payload["owners"][0]))
    parse_error(payload)

    for mutation in (
        lambda data: data["owners"][1]["parameter"].update(name="other"),
        lambda data: data["owners"][1]["parameter"].update(pattern=".*"),
        lambda data: data["owners"][1]["parameter"].update(source="inferred"),
        lambda data: data["owners"][1]["parameter"].update(unknown=True),
        lambda data: data["owners"][1].update(owner_kind="canonical_vault"),
        lambda data: data["owners"][1].update(locator="vault://project/fixed"),
        lambda data: data["owners"][1]["active_writer"].update(writer_locator="vault://project/fixed"),
        lambda data: data["owners"][0].update(locator="vault://meta/{other}"),
        lambda data: data["owners"][0]["active_writer"].update(writer_locator="vault://meta/{other}"),
    ):
        payload = valid_payload()
        mutation(payload)
        parse_error(payload)


def test_private_timestamp_accepts_aware_datetime_and_rejects_naive() -> None:
    assert ownership._timestamp(NOW, "time", None) is NOW
    with pytest.raises(OwnershipCatalogError, match="UTC offset"):
        ownership._timestamp(datetime(2026, 8, 29), "time", None)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("state", "inactive", "state"),
        ("generation", True, "generation"),
        ("generation", 0, "generation"),
        ("writer_locator", "file://bad", "writer_locator"),
        ("valid_from", datetime(2026, 8, 29), "valid_from"),
        ("expires_at", datetime(2026, 8, 30), "expires_at"),
        ("expires_at", datetime(2026, 8, 28, tzinfo=timezone.utc), "later than"),
        ("required_preimage_sha256", "bad", "required_preimage_sha256"),
    ],
)
def test_writer_generation_constructor_and_replace_reject_invalid_values(
    field: str, value: object, message: str
) -> None:
    writer = parse_payload().entries[0].active_writer
    with pytest.raises(ValueError, match=message):
        replace(writer, **{field: value})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("owner_id", "", "owner_id"),
        ("owner_kind", [], "owner_kind"),
        ("owner_kind", "unknown", "owner_kind"),
        ("locator", "file://bad", "locator"),
        ("responsibilities", [], "non-empty tuple"),
        ("responsibilities", (), "non-empty tuple"),
        ("responsibilities", ("unknown",), "unknown key"),
        ("responsibilities", ("system_decisions", "system_decisions"), "duplicate key"),
        ("active_writer", object(), "active_writer"),
        ("project_parameter", "other", "project_parameter"),
        ("project_parameter", "project_id", "disagree"),
        ("locator", "vault://meta/{other}", "project parameter or locator|project template"),
    ],
)
def test_ownership_entry_constructor_and_replace_reject_invalid_values(
    field: str, value: object, message: str
) -> None:
    entry = parse_payload().entries[0]
    with pytest.raises(ValueError, match=message):
        replace(entry, **{field: value})


def test_ownership_entry_rejects_invalid_writer_template_and_parameterized_mismatch() -> None:
    catalog = parse_payload()
    ordinary, project = catalog.entries
    templated_writer = replace(ordinary.active_writer, writer_locator="vault://meta/{project_id}")
    with pytest.raises(ValueError, match="writer_locator"):
        replace(ordinary, active_writer=templated_writer)
    with pytest.raises(ValueError, match="disagree"):
        replace(project, project_parameter=None)
    with pytest.raises(ValueError, match="project parameter or locator|project template"):
        replace(project, locator="vault://project/fixed")
    fixed_writer = replace(project.active_writer, writer_locator="vault://project/fixed")
    with pytest.raises(ValueError, match="writer_locator"):
        replace(project, active_writer=fixed_writer)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("schema", "brain.ownership-catalog/v2", "schema"),
        ("catalog_id", "", "catalog_id"),
        ("catalog_version", True, "catalog_version"),
        ("catalog_version", 2, "catalog_version"),
        ("entries", [], "non-empty tuple"),
        ("entries", (), "non-empty tuple"),
        ("entries", (object(),), "OwnershipEntry"),
        ("source_sha256", "bad", "source_sha256"),
    ],
)
def test_ownership_catalog_constructor_and_replace_reject_invalid_values(
    field: str, value: object, message: str
) -> None:
    catalog = parse_payload()
    with pytest.raises(ValueError, match=message):
        replace(catalog, **{field: value})


def test_ownership_catalog_rejects_duplicate_and_incomplete_authority() -> None:
    catalog = parse_payload()
    ordinary, project = catalog.entries
    with pytest.raises(ValueError, match="duplicate owner_id"):
        replace(catalog, entries=(ordinary, replace(project, owner_id=ordinary.owner_id)))
    duplicate_claim = replace(
        project,
        responsibilities=(*project.responsibilities, ordinary.responsibilities[0]),
    )
    with pytest.raises(ValueError, match="duplicate responsibility"):
        replace(catalog, entries=(ordinary, duplicate_claim))
    incomplete = replace(
        ordinary,
        responsibilities=ordinary.responsibilities[1:],
    )
    with pytest.raises(ValueError, match="every responsibility"):
        replace(catalog, entries=(incomplete, project))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("responsibility", [], "responsibility"),
        ("responsibility", "unknown", "responsibility"),
        ("owner_id", "", "owner_id"),
        ("owner_locator", "file://bad", "owner_locator"),
        ("owner_locator", "vault://project/{project_id}", "owner_locator"),
        ("writer_locator", "file://bad", "writer_locator"),
        ("writer_locator", "vault://project/{project_id}", "writer_locator"),
        ("writer_generation", True, "writer_generation"),
        ("writer_generation", 0, "writer_generation"),
        ("catalog_id", "", "catalog_id"),
        ("catalog_sha256", "bad", "catalog_sha256"),
    ],
)
def test_ownership_resolution_constructor_and_replace_reject_invalid_values(
    field: str, value: object, message: str
) -> None:
    resolution = resolve_ownership(parse_payload(), "system_decisions", project_id=None, at=NOW)
    assert isinstance(resolution, OwnershipResolution)
    with pytest.raises(ValueError, match=message):
        replace(resolution, **{field: value})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("responsibility", [], "responsibility"),
        ("code", "ownership_unknown", "code"),
        ("catalog_id", "", "catalog_id"),
        ("detail", "", "detail"),
    ],
)
def test_ownership_refusal_constructor_and_replace_reject_invalid_values(
    field: str, value: object, message: str
) -> None:
    refusal = resolve_ownership(parse_payload(), "unknown", project_id=None, at=NOW)
    assert isinstance(refusal, OwnershipRefusal)
    with pytest.raises(ValueError, match=message):
        replace(refusal, **{field: value})


def test_direct_constructors_accept_valid_boundary_values() -> None:
    catalog = parse_payload()
    writer = WriterGeneration(
        state="active",
        generation=2,
        writer_locator="vault://brain-core",
        valid_from=NOW,
        expires_at=NOW + timedelta(seconds=1),
        required_preimage_sha256="b" * 64,
    )
    entry = OwnershipEntry(
        owner_id="replacement",
        owner_kind="canonical_vault",
        locator="vault://replacement",
        responsibilities=catalog.entries[0].responsibilities,
        active_writer=writer,
        project_parameter=None,
    )
    assert entry.active_writer.required_preimage_sha256 == "b" * 64
    refusal = OwnershipRefusal("unknown", OwnershipRefusalCode.UNKNOWN, None, "unknown responsibility")
    assert refusal.catalog_id is None
