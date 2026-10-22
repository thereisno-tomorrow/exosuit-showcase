from __future__ import annotations

import json
import tomllib
from pathlib import Path, PurePosixPath

from gremlin_town.mcp.brain_assembly import (
    _GITHUB_OPERATIONS,
    _PROJECT_OPERATIONS,
    _SITREP_OPERATIONS,
    load_owner_policy,
)
from gremlin_town.mcp.brain_runtime import BrainOperation
from gremlin_town.runtime import (
    PARTICIPANT_CONFIG_KEYS,
    ROUTE_CONFIG_KEYS,
    RUNTIME_CONFIG_SCHEMA,
)


ROOT = Path(__file__).resolve().parents[2]


def _toml(path: Path) -> dict:
    with path.open("rb") as handle:
        return tomllib.load(handle)


def test_deployment_template_contains_only_runtime_consumed_keys() -> None:
    for name in ("config.toml.example", "config.recovery.toml.example"):
        config = _toml(ROOT / "deploy" / name)
        assert set(config) <= set(RUNTIME_CONFIG_SCHEMA)
        for table, values in config.items():
            assert isinstance(values, dict)
            assert set(values) <= RUNTIME_CONFIG_SCHEMA[table]
        assert "limits" not in config

    enrollment = _toml(ROOT / "deploy" / "participants.toml.example")
    assert set(enrollment) == {"participant", "route"}
    assert all(set(item) <= PARTICIPANT_CONFIG_KEYS for item in enrollment["participant"])
    assert all(set(item) == ROUTE_CONFIG_KEYS for item in enrollment["route"])


def test_brain_policy_template_enables_every_supported_brain_home_operation() -> None:
    # A release that adds a Brain operation must add it to the Brain Home
    # (personal/cross-project) owners here too, or it is silently refused for
    # every agent once deployed (found live 2026-09-26; see operations.md).
    policy = load_owner_policy(ROOT / "deploy" / "brain-policy.json.example")
    by_kind = {owner.selector.kind: owner for owner in policy.owners}
    assert {"personal", "cross-project"} <= set(by_kind)
    # Sitreps (b-project-sitreps.md): every agent Nic uses reads them through the cross-project
    # owner's readers, so only that owner carries sitrep.read; it acts once [sitreps] is on.
    extra = {"personal": frozenset(), "cross-project": _SITREP_OPERATIONS}
    for kind in ("personal", "cross-project"):
        owner = by_kind[kind]
        expected = _GITHUB_OPERATIONS if kind=='personal' else _GITHUB_OPERATIONS - {BrainOperation.WORK}
        missing = expected - owner.operations
        assert not missing, f"{kind} owner in the template is missing {sorted(missing)}"
        assert owner.operations == expected | extra[kind]
        assert (BrainOperation.WORK in owner.operations) is (kind=='personal')
    # A project owner, if the template shows one, never exceeds what project
    # enrollment supports, and since 28 Sep 2026 (ROA-15) it includes continuity.list,
    # so a new chat on the project can find where earlier chats stopped.
    if "project" in by_kind:
        assert by_kind["project"].operations <= _PROJECT_OPERATIONS
        assert BrainOperation.CONTINUITY_LIST in by_kind["project"].operations


def test_enrollment_template_has_exact_named_internal_and_test_routes() -> None:
    enrollment = _toml(ROOT / "deploy" / "participants.toml.example")
    participants = {item["id"]: item for item in enrollment["participant"]}
    assert set(participants) == {
        "local-codex",
        "hermes-wa",
        "fleet-boss-grok",
        "demo-operator",
    }
    for participant in participants.values():
        assert participant["address"] == "agenthub@example.invalid"
        assert participant["projects"] == ["gremlin-town"]
        assert set(participant["sensitivities"]) == {"public-test", "internal"}
        assert participant["token_sha256"].startswith("REPLACE_")
    # D18 (26 Sep): every one of Nic's own agents reads every request in a
    # shared project, and D19 lets it check a result it didn't return; report,
    # reply and cancel stay requester/recipient/operator-only.
    for name in ("local-codex", "hermes-wa", "fleet-boss-grok"):
        assert participants[name]["read_all_requests"] is True
    peers = {"local-codex", "hermes-wa", "fleet-boss-grok"}
    expected_routes = {
        ("gremlin-town", source, destination, sensitivity)
        for source in peers
        for destination in peers
        if source != destination
        for sensitivity in {"public-test", "internal"}
    }
    actual_routes = {
        (item["project"], item["source"], item["destination"], item["sensitivity"])
        for item in enrollment["route"]
    }
    assert actual_routes == expected_routes
    assert len(enrollment["route"]) == 12
    assert all("demo-operator" not in route[1:3] for route in actual_routes)


def test_adapter_install_manifests_reference_existing_sources_and_reserved_endpoints() -> None:
    for manifest_name in (
        "adapters/cloud/install-manifest.json",
        "adapters/hermes/install-manifest.json",
    ):
        manifest = json.loads((ROOT / manifest_name).read_text(encoding="utf-8"))
        for entry in manifest["files"]:
            source = (ROOT / entry["source"]).resolve()
            assert source.is_relative_to(ROOT.resolve()) and source.is_file()
            assert PurePosixPath(entry["destination"]).is_absolute()
    for relative in ("adapters/hermes/settings.json", "adapters/hermes/memory_brain/settings.json"):
        settings = json.loads((ROOT / relative).read_text(encoding="utf-8"))
        assert settings["hub_url"] == "https://hub.example.invalid"


def test_backup_schema_allows_every_review_setting() -> None:
    # The backup validator (deploy/validate-backup.py) refuses any config field missing from
    # RUNTIME_CONFIG_SCHEMA. brain-first-01 added [review] sweep_participant to review/settings.py only,
    # so every backup snapshot was refused from 29 Sep 15:05 UTC until links-01.
    from gremlin_town.review.settings import ALLOWED_KEYS

    assert RUNTIME_CONFIG_SCHEMA["review"] == ALLOWED_KEYS


def test_placement_config_keys_match_the_runtime_schema() -> None:
    """Placement slice 7: every [placement] key the settings accept is in RUNTIME_CONFIG_SCHEMA, so the
    backup validator accepts the live config that turns placement on (the gap brain-first-01 found for
    [review] sweep_participant)."""

    from gremlin_town.review.settings import PLACEMENT_ALLOWED_KEYS
    from gremlin_town.runtime import RUNTIME_CONFIG_SCHEMA

    assert RUNTIME_CONFIG_SCHEMA["placement"] == PLACEMENT_ALLOWED_KEYS


def test_storage_layer_two_and_work_keys_validate_in_a_real_fixture_backup(tmp_path):
    import importlib.util
    import hashlib
    from tests.integration.test_restore_helper import _snapshot
    config = _toml(ROOT / "deploy" / "config.toml.example")
    assert config["brain_core"]["archive_superseded"] is False
    assert config["work"] == {"door_participant": "hermes-wa"}
    assert "archive_superseded" in RUNTIME_CONFIG_SCHEMA["brain_core"]
    assert RUNTIME_CONFIG_SCHEMA["work"] == {"door_participant", "review_card", "dashboard"}
    spec = importlib.util.spec_from_file_location("w2_backup_validator", ROOT / "deploy" / "validate-backup.py")
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    snapshot = _snapshot(tmp_path)
    with (snapshot / "config.toml").open("a", encoding="utf-8") as file:
        file.write('[brain_core]\narchive_superseded = true\n[work]\ndoor_participant = "hermes-wa"\n')
    (snapshot / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256((snapshot / name).read_bytes()).hexdigest()}  {name}\n"
        for name in sorted(validator.HASHED_FILES)
    ), encoding="ascii")
    validator.validate_directory(snapshot)


def test_config_with_work_starts_and_refuses_unknown_work_keys(tmp_path):
    import pytest
    from gremlin_town.runtime import ConfigurationError, build_runtime
    from tests.integration.test_mcp_runtime import mcp_config
    from gremlin_town.transport import FakeTransport
    config, _ = mcp_config(tmp_path)
    with config.open("a", encoding="utf-8") as file:
        file.write('\n[work]\ndoor_participant = "hermes-wa"\n')
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    runtime.close()
    with config.open("a", encoding="utf-8") as file:
        file.write('enabled = true\n')
    with pytest.raises(ConfigurationError, match="work"):
        build_runtime(config, provider=FakeTransport(), start_worker=False)
