"""The hub's project owners, generated from the PC connector's enrollment at release time (ROA-72)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from gremlin_town.mcp.brain_assembly import load_owner_policy

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "brain_policy_projects.py"
spec = importlib.util.spec_from_file_location("brain_policy_projects", SCRIPT)
assert spec is not None and spec.loader is not None
script = importlib.util.module_from_spec(spec)
spec.loader.exec_module(script)

AGENTS = ["chatgpt-web", "claude-web", "fleet-boss-grok", "hermes-wa", "local-codex"]
BRAIN_HOME_OPERATIONS = [
    "continuity.append", "continuity.close", "continuity.list", "continuity.recover", "continuity.start",
    "decision.record", "memory.change", "memory.list", "memory.read", "memory.save", "memory.search",
]


def live_like_policy() -> dict:
    """The shape of the hub's policy on 28 Sep 2026 (revision bcgt-shared-brain-20260928-02)."""

    return {
        "schema": "gt.brain-owner-policy/v1",
        "revision": "bcgt-shared-brain-20260928-02",
        "owners": [
            {"kind": "personal", "project_id": None, "owner_id": "brain-home", "owner_locator": "vault://brain-home",
             "readers": AGENTS + ["exosuit-review"], "writers": AGENTS, "operations": BRAIN_HOME_OPERATIONS,
             "session_id": None},
            {"kind": "cross-project", "project_id": None, "owner_id": "brain-home",
             "owner_locator": "vault://brain-home", "readers": AGENTS, "writers": AGENTS,
             "operations": BRAIN_HOME_OPERATIONS, "session_id": None},
            {"kind": "project", "project_id": "vault_metavault", "owner_id": "vault_metavault",
             "owner_locator": "vault://vault_metavault", "readers": AGENTS, "writers": AGENTS,
             "operations": list(script.PROJECT_OPERATIONS), "session_id": "bcgt-chatgpt-metavault-v1"},
            {"kind": "project", "project_id": "vault_gtbc_plans", "owner_id": "vault_gtbc_plans",
             "owner_locator": "vault://vault_gtbc_plans", "readers": AGENTS,
             "writers": ["chatgpt-web", "claude-web"], "operations": ["context.retrieve"],
             "session_id": "bcgt-chatgpt-gtbc-plans-v1"},
        ],
        "subject_scopes": {"local-codex": ["bc:read", "bc:write"]},
    }


def enrollment(*registry: str, config: tuple[str, ...] = ("vault_gtbc_plans",)) -> dict:
    return {
        "node": "nic-pc",
        "projects": [{"id": item, "source": "registry"} for item in registry]
        + [{"id": item, "source": "config"} for item in config],
        "skipped": [],
    }


def test_every_registry_vault_gets_the_same_agents_and_hand_set_projects_stay_as_they_are(tmp_path: Path) -> None:
    policy, changes = script.generate(
        live_like_policy(), enrollment("vault_metavault", "vault_exosuit", "vault_crypto"), "bcgt-shared-brain-20260929-01",
    )

    assert changes == {
        "added": ["vault_crypto", "vault_exosuit"],
        "updated": [],
        "kept": ["vault_gtbc_plans", "vault_metavault"],
        "dropped": [],
    }
    owners = {owner["project_id"]: owner for owner in policy["owners"] if owner["kind"] == "project"}
    assert owners["vault_exosuit"] == {
        "kind": "project", "project_id": "vault_exosuit", "owner_id": "vault_exosuit",
        "owner_locator": "vault://vault_exosuit", "readers": AGENTS, "writers": AGENTS,
        "operations": list(script.PROJECT_OPERATIONS), "session_id": "gt-vault_exosuit-v1",
    }
    assert owners["vault_metavault"]["session_id"] == "bcgt-chatgpt-metavault-v1"
    assert owners["vault_gtbc_plans"] == live_like_policy()["owners"][3]
    assert policy["owners"][:2] == live_like_policy()["owners"][:2]
    assert policy["subject_scopes"] == live_like_policy()["subject_scopes"]
    assert policy["revision"] == "bcgt-shared-brain-20260929-01"
    path = tmp_path / "brain-policy.json"
    path.write_text(json.dumps(policy), encoding="utf-8")
    loaded = load_owner_policy(path)
    assert sorted(owner.selector.project_id for owner in loaded.owners if owner.selector.kind == "project") == [
        "vault_crypto", "vault_exosuit", "vault_gtbc_plans", "vault_metavault",
    ]


def test_an_owner_the_connector_no_longer_serves_is_dropped_and_a_drifted_one_is_updated() -> None:
    before = live_like_policy()
    before["owners"][2]["writers"] = ["chatgpt-web"]
    policy, changes = script.generate(before, enrollment("vault_metavault", config=()), "rev-2")
    assert changes["dropped"] == ["vault_gtbc_plans"]
    assert changes["updated"] == ["vault_metavault"]
    assert [owner.get("project_id") for owner in policy["owners"]] == [None, None, "vault_metavault"]


def test_the_revision_must_move_and_the_agents_come_from_one_cross_project_owner() -> None:
    with pytest.raises(script.PolicyError, match="revision"):
        script.generate(live_like_policy(), enrollment("vault_exosuit"), "bcgt-shared-brain-20260928-02")
    no_cross = live_like_policy()
    no_cross["owners"] = [owner for owner in no_cross["owners"] if owner["kind"] != "cross-project"]
    with pytest.raises(script.PolicyError, match="cross-project"):
        script.generate(no_cross, enrollment("vault_exosuit"), "rev-2")


def test_main_writes_the_checked_policy_and_names_the_changes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    current = tmp_path / "policy.json"
    current.write_text(json.dumps(live_like_policy()), encoding="utf-8")
    projects = tmp_path / "projects.json"
    projects.write_text(json.dumps(enrollment("vault_metavault", "vault_exosuit")), encoding="utf-8")
    output = tmp_path / "new-policy.json"
    assert script.main(["--policy", str(current), "--projects", str(projects), "--revision", "rev-3",
                        "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["revision"] == "rev-3"
    assert "added: 1 - vault_exosuit" in capsys.readouterr().err
    projects.write_text(json.dumps({"projects": []}), encoding="utf-8")
    assert script.main(["--policy", str(current), "--projects", str(projects), "--revision", "rev-4"]) == 1
