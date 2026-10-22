from __future__ import annotations

import importlib
import socket
import time
import urllib.request
from dataclasses import replace
from pathlib import Path

import pytest

from brain_core.errors import BrainError, ErrorCode
from brain_core.skills import (
    DESCRIPTION_TOKEN_WEIGHT,
    EXACT_TRIGGER_WEIGHT,
    EXPLICIT_INVOCATION_WEIGHT,
    MAX_FRONTMATTER_BYTES,
    TRIGGER_TOKEN_WEIGHT,
    SkillCatalog,
    SkillMetadata,
    SkillRoute,
    SkillRouteRequest,
    load_selected_instructions,
    parse_skill_metadata,
    read_skill_metadata,
    route_skill,
)

FIXTURES = Path(__file__).parents[1] / "fixtures" / "skills"
GOOD_FIXTURES = (
    "arguments",
    "browser-reader",
    "job-match",
    "tie-alpha",
    "tie-beta",
)


def _fixture_paths(*names: str) -> list[Path]:
    selected = names or GOOD_FIXTURES
    return [FIXTURES / name / "SKILL.md" for name in selected]


def _catalog(*names: str) -> SkillCatalog:
    return SkillCatalog.from_paths(_fixture_paths(*names))


def _metadata(skill_id: str = "sample", **changes: object) -> SkillMetadata:
    base = SkillMetadata(
        schema="brain.skill/v1",
        id=skill_id,
        version="1.0.0",
        description="Handle supplied evidence.",
        positive_triggers=("handle evidence",),
        negative_triggers=(),
        supported_modes=("think",),
        required_authority=(),
        dependencies=(),
        host_capabilities=(),
        portability="portable",
        owner="brain-home",
    )
    return replace(base, **changes)


def _frontmatter(**changes: object) -> str:
    values: dict[str, object] = {
        "schema": "brain.skill/v1",
        "id": "sample",
        "version": '"1.0.0"',
        "description": "A sample skill.",
        "positive_triggers": "[sample this]",
        "negative_triggers": "[]",
        "supported_modes": "[think]",
        "required_authority": "[]",
        "dependencies": "[]",
        "host_capabilities": "[]",
        "portability": "portable",
        "owner": "brain-home",
    }
    values.update(changes)
    return "\n".join(f"{key}: {value}" for key, value in values.items()) + "\n"


def _write_skill(path: Path, frontmatter: str | None = None, body: bytes = b"BODY\n") -> None:
    header = (frontmatter or _frontmatter()).encode("utf-8")
    path.write_bytes(b"---\n" + header + b"---\n" + body)


def test_positive_trigger_routes_deterministically_from_metadata_only() -> None:
    catalog = _catalog("arguments")
    request = SkillRouteRequest(intent="Please BUILD an argument, now.", mode="think")

    first = route_skill(catalog, request)
    second = route_skill(catalog, request)

    assert first == second
    assert first.status == "selected"
    assert first.selected is not None
    assert first.selected.id == "arguments"
    assert first.selected.matched_positive_triggers == ("build an argument",)
    assert first.selected.score >= EXACT_TRIGGER_WEIGHT + 3 * TRIGGER_TOKEN_WEIGHT
    assert "ARGUMENTS-INSTRUCTION-BODY" not in repr(catalog)
    assert "ARGUMENTS-INSTRUCTION-BODY" not in repr(first)


def test_negative_trigger_blocks_automatic_route_but_explicit_invocation_wins() -> None:
    catalog = _catalog("arguments")
    automatic = route_skill(
        catalog,
        SkillRouteRequest(
            intent="Build an argument, then write production code.",
            mode="think",
        ),
    )
    explicit = route_skill(
        catalog,
        SkillRouteRequest(
            intent="Write production code.",
            mode="think",
            explicit_skill_id="arguments",
        ),
    )

    assert automatic == SkillRoute(
        status="not_found", reason="No positive skill trigger matched"
    )
    assert explicit.status == "selected"
    assert explicit.selected is not None
    assert explicit.selected.explicit is True
    assert explicit.selected.score == EXPLICIT_INVOCATION_WEIGHT
    assert explicit.selected.matched_positive_triggers == ()


def test_weighted_score_selects_unique_winner_and_constants_are_nonzero() -> None:
    alpha = _metadata(
        "alpha",
        description="Handle special evidence.",
    )
    beta = _metadata(
        "beta",
        description="Handle ordinary evidence.",
    )
    route = route_skill(
        SkillCatalog.from_metadata((beta, alpha)),
        SkillRouteRequest(intent="handle evidence special", mode="think"),
    )

    assert route.status == "selected"
    assert route.selected is not None
    assert route.selected.id == "alpha"
    assert route.selected.score == (
        EXACT_TRIGGER_WEIGHT + 2 * TRIGGER_TOKEN_WEIGHT + 3 * DESCRIPTION_TOKEN_WEIGHT
    )
    assert EXACT_TRIGGER_WEIGHT > TRIGGER_TOKEN_WEIGHT > DESCRIPTION_TOKEN_WEIGHT > 0


def test_tied_top_candidates_are_returned_plainly_in_id_order() -> None:
    route = route_skill(
        _catalog("tie-beta", "tie-alpha"),
        SkillRouteRequest(intent="Summarize evidence", mode="think"),
    )

    assert route.status == "ambiguous"
    assert route.selected is None
    assert [candidate.id for candidate in route.candidates] == ["tie-alpha", "tie-beta"]
    assert len({candidate.score for candidate in route.candidates}) == 1


def test_unavailable_host_reports_every_capability_gate() -> None:
    catalog = _catalog("browser-reader")
    unavailable = route_skill(
        catalog,
        SkillRouteRequest(intent="read linkedin", mode="think"),
    )

    assert unavailable.status == "unavailable"
    assert unavailable.unavailable[0].reasons == (
        "unsupported-mode:think",
        "missing-authority:session-read",
        "missing-dependency:browser-session",
        "missing-host-capability:host-browser",
    )

    selected = route_skill(
        catalog,
        SkillRouteRequest(
            intent="read linkedin",
            mode="research",
            authority=frozenset({"session-read"}),
            available_dependencies=frozenset({"browser-session"}),
            host_capabilities=frozenset({"host-browser"}),
        ),
    )
    assert selected.status == "selected"


def test_project_skill_requires_matching_owner_and_authority() -> None:
    catalog = _catalog("job-match")
    wrong_owner = route_skill(
        catalog,
        SkillRouteRequest(
            intent="score this job",
            mode="think",
            authority=frozenset({"career-read"}),
            project_owner="augmentus",
        ),
    )
    assert wrong_owner.status == "unavailable"
    assert wrong_owner.unavailable[0].reasons == ("wrong-project-owner:career",)

    right_owner = route_skill(
        catalog,
        SkillRouteRequest(
            intent="score this job",
            mode="think",
            authority=frozenset({"career-read"}),
            project_owner="career",
        ),
    )
    assert right_owner.status == "selected"
    assert right_owner.selected is not None
    assert right_owner.selected.owner == "career"


def test_explicit_unknown_and_unavailable_do_not_fall_back() -> None:
    catalog = _catalog("arguments", "browser-reader")
    unknown = route_skill(
        catalog,
        SkillRouteRequest(intent="build an argument", mode="think", explicit_skill_id="missing"),
    )
    blocked = route_skill(
        catalog,
        SkillRouteRequest(
            intent="build an argument",
            mode="think",
            explicit_skill_id="browser-reader",
        ),
    )

    assert unknown.status == "not_found"
    assert unknown.reason == "Unknown skill: missing"
    assert blocked.status == "unavailable"
    assert blocked.unavailable[0].id == "browser-reader"


def test_instruction_body_requires_explicit_load_after_unique_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog("arguments", "tie-alpha", "tie-beta")
    selected = route_skill(
        catalog,
        SkillRouteRequest(intent="build an argument", mode="think"),
    )
    ambiguous = route_skill(
        catalog,
        SkillRouteRequest(intent="summarize evidence", mode="think"),
    )
    original_open = Path.open
    calls = 0

    def counted_open(self: Path, *args: object, **kwargs: object):
        nonlocal calls
        calls += 1
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counted_open)
    with pytest.raises(BrainError) as not_requested:
        load_selected_instructions(catalog, selected)
    with pytest.raises(BrainError) as not_unique:
        load_selected_instructions(catalog, ambiguous, requested=True)
    assert calls == 0
    assert not_requested.value.code is ErrorCode.INVALID_ARGUMENT
    assert not_unique.value.code is ErrorCode.CAPABILITY_UNAVAILABLE

    body = load_selected_instructions(catalog, selected, requested=True)
    assert calls == 1
    assert "ARGUMENTS-INSTRUCTION-BODY" in body


def test_catalog_from_metadata_cannot_disclose_an_instruction_body() -> None:
    catalog = SkillCatalog.from_metadata((_metadata(),))
    route = route_skill(
        catalog,
        SkillRouteRequest(intent="handle evidence", mode="think"),
    )
    with pytest.raises(BrainError) as error:
        load_selected_instructions(catalog, route, requested=True)
    assert error.value.code is ErrorCode.CAPABILITY_UNAVAILABLE
    assert error.value.details == {"skill_id": "sample"}


def test_forged_nonpositive_route_cannot_disclose_instructions() -> None:
    catalog = _catalog("arguments")
    real = route_skill(
        catalog,
        SkillRouteRequest(intent="build an argument", mode="think"),
    )
    assert real.selected is not None
    nonpositive = replace(
        real.selected,
        matched_positive_triggers=(),
        explicit=False,
    )
    forged = SkillRoute(
        status="selected",
        selected=nonpositive,
        candidates=(nonpositive,),
    )
    with pytest.raises(BrainError) as error:
        load_selected_instructions(catalog, forged, requested=True)
    assert error.value.code is ErrorCode.CAPABILITY_UNAVAILABLE


def test_catalog_reads_bounded_frontmatter_not_body(tmp_path: Path) -> None:
    path = tmp_path / "SKILL.md"
    _write_skill(path, body=b"\xff" * 1_000_000)

    metadata = read_skill_metadata(path)
    catalog = SkillCatalog.from_paths((path,))
    route = route_skill(
        catalog,
        SkillRouteRequest(intent="sample this", mode="think"),
    )

    assert metadata.id == "sample"
    assert route.status == "selected"
    with pytest.raises(BrainError) as error:
        load_selected_instructions(catalog, route, requested=True)
    assert error.value.code is ErrorCode.SOURCE_BINARY


def test_instruction_load_detects_changed_or_unreadable_source(tmp_path: Path) -> None:
    changed_path = tmp_path / "changed.md"
    _write_skill(changed_path)
    changed_catalog = SkillCatalog.from_paths((changed_path,))
    changed_route = route_skill(
        changed_catalog,
        SkillRouteRequest(intent="sample this", mode="think"),
    )
    changed_path.write_text(
        changed_path.read_text(encoding="utf-8").replace('version: "1.0.0"', 'version: "2.0.0"'),
        encoding="utf-8",
    )
    with pytest.raises(BrainError) as changed:
        load_selected_instructions(changed_catalog, changed_route, requested=True)
    assert changed.value.code is ErrorCode.SOURCE_CHANGED

    missing_path = tmp_path / "missing.md"
    _write_skill(missing_path)
    missing_catalog = SkillCatalog.from_paths((missing_path,))
    missing_route = route_skill(
        missing_catalog,
        SkillRouteRequest(intent="sample this", mode="think"),
    )
    missing_path.unlink()
    with pytest.raises(BrainError) as missing:
        load_selected_instructions(missing_catalog, missing_route, requested=True)
    assert missing.value.code is ErrorCode.SOURCE_UNREADABLE


def test_malformed_fixture_and_duplicate_ids_fail_loudly() -> None:
    with pytest.raises(BrainError) as malformed:
        SkillCatalog.from_paths(_fixture_paths("malformed"))
    assert malformed.value.code is ErrorCode.CONTRACT_INVALID
    assert malformed.value.details["field"] == "host_capabilities"

    duplicate = _metadata()
    with pytest.raises(BrainError) as error:
        SkillCatalog.from_metadata((duplicate, duplicate))
    assert error.value.code is ErrorCode.DUPLICATE_ID


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"schema": "brain.skill/v2"}, "schema"),
        ({"id": "Not Valid"}, "id"),
        ({"version": "1.0"}, "version"),
        ({"description": "|\n  two\n  lines"}, "description"),
        ({"positive_triggers": "[]"}, "positive_triggers"),
        ({"positive_triggers": "not-a-list"}, "positive_triggers"),
        ({"negative_triggers": "[same, SAME]"}, "negative_triggers"),
        ({"supported_modes": "[]"}, "supported_modes"),
        ({"required_authority": "[read, 1]"}, "required_authority[1]"),
        ({"portability": "telepathic"}, "portability"),
        ({"owner": ""}, "owner"),
        ({"surprise": "true"}, "unknown fields"),
    ],
)
def test_strict_metadata_contract_rejects_malformed_fields(
    changes: dict[str, object], field: str
) -> None:
    with pytest.raises(BrainError) as error:
        parse_skill_metadata(_frontmatter(**changes), "fixture")
    assert error.value.code is ErrorCode.CONTRACT_INVALID
    assert error.value.details["field"] == field


@pytest.mark.parametrize(
    "frontmatter",
    [
        _frontmatter() + "owner: duplicate\n",
        _frontmatter().replace("id: sample", "id: [unterminated"),
    ],
)
def test_strict_yaml_errors_are_preserved(frontmatter: str) -> None:
    with pytest.raises(BrainError) as error:
        parse_skill_metadata(frontmatter, "fixture")
    assert error.value.code is ErrorCode.YAML_INVALID


def test_nested_trigger_contract_rejects_mixed_unknown_and_nonmapping_forms() -> None:
    base = _frontmatter(
        positive_triggers=None,
        negative_triggers=None,
        triggers="{positive: [sample this], negative: []}",
    ).replace("positive_triggers: None\n", "").replace("negative_triggers: None\n", "")
    assert parse_skill_metadata(base).positive_triggers == ("sample this",)
    negative_null = base.replace(
        "triggers: {positive: [sample this], negative: []}",
        "triggers: {positive: [sample this], negative: null}",
    )
    assert parse_skill_metadata(negative_null).negative_triggers == ()

    cases = (
        _frontmatter(triggers="{positive: [sample this] }"),
        base.replace("triggers: {positive: [sample this], negative: []}", "triggers: []"),
        base.replace(
            "triggers: {positive: [sample this], negative: []}",
            "triggers: {positive: [sample this], maybe: []}",
        ),
    )
    expected_fields = ("triggers", "triggers", "triggers")
    for text, field in zip(cases, expected_fields, strict=True):
        with pytest.raises(BrainError) as error:
            parse_skill_metadata(text)
        assert error.value.details["field"] == field


def test_frontmatter_boundaries_and_read_errors(tmp_path: Path) -> None:
    missing_open = tmp_path / "missing-open.md"
    missing_open.write_text(_frontmatter(), encoding="utf-8")
    with pytest.raises(BrainError) as error:
        read_skill_metadata(missing_open)
    assert error.value.details["actual"] == "missing"

    missing_close = tmp_path / "missing-close.md"
    missing_close.write_text("---\n" + _frontmatter(), encoding="utf-8")
    with pytest.raises(BrainError) as error:
        read_skill_metadata(missing_close)
    assert error.value.details["actual"] == "end of file"

    oversized = tmp_path / "oversized.md"
    oversized.write_text("---\n" + ("#" * MAX_FRONTMATTER_BYTES), encoding="utf-8")
    with pytest.raises(BrainError) as error:
        read_skill_metadata(oversized)
    assert error.value.details["max_bytes"] == MAX_FRONTMATTER_BYTES

    invalid_utf8 = tmp_path / "invalid-utf8.md"
    invalid_utf8.write_bytes(b"---\n\xff\n---\nBODY")
    with pytest.raises(BrainError) as error:
        read_skill_metadata(invalid_utf8)
    assert error.value.code is ErrorCode.CONTRACT_INVALID

    dots = tmp_path / "dots.md"
    dots.write_text("---\n" + _frontmatter() + "...\nBODY", encoding="utf-8")
    assert read_skill_metadata(dots).id == "sample"

    with pytest.raises(BrainError) as error:
        read_skill_metadata(dots, max_frontmatter_bytes=0)
    assert error.value.code is ErrorCode.INVALID_ARGUMENT

    with pytest.raises(BrainError) as error:
        read_skill_metadata(dots, max_frontmatter_bytes=3)
    assert error.value.details["max_bytes"] == 3

    with pytest.raises(BrainError) as error:
        read_skill_metadata(tmp_path / "absent.md")
    assert error.value.code is ErrorCode.SOURCE_UNREADABLE


def test_route_makes_zero_network_model_or_provider_calls_and_meets_p95(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = _catalog("arguments", "tie-alpha", "tie-beta")
    request = SkillRouteRequest(intent="build an argument", mode="think")

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("external call attempted")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    monkeypatch.setattr(importlib, "import_module", forbidden)

    durations: list[int] = []
    for _ in range(500):
        started = time.perf_counter_ns()
        assert route_skill(catalog, request).status == "selected"
        durations.append(time.perf_counter_ns() - started)

    p95 = sorted(durations)[int(len(durations) * 0.95) - 1]
    assert p95 < 50_000_000


def test_catalog_metadata_lookup_and_sorted_unavailable_receipts() -> None:
    first = _metadata(
        "zeta",
        required_authority=("zeta-read",),
    )
    second = _metadata(
        "alpha",
        required_authority=("alpha-read",),
    )
    catalog = SkillCatalog.from_metadata((first, second))
    route = route_skill(
        catalog,
        SkillRouteRequest(intent="handle evidence", mode="think"),
    )

    assert [item.id for item in catalog.entries] == ["alpha", "zeta"]
    assert catalog.metadata("alpha") == second
    assert catalog.metadata("absent") is None
    assert route.status == "unavailable"
    assert [item.id for item in route.unavailable] == ["alpha", "zeta"]
