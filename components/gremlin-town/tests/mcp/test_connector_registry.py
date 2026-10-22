"""The PC connector enrolls every vault in MetaVault's registry (ROA-72).

docs/product/steps/b-notes-in-each-vault.md: each registered vault whose folder is on the PC is
served as ``vault_<name>``; nothing is written into a vault to enroll it; agents read its declared
exports plus its own notes; notes land in its ``ops/sessions/``.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).parent))
from brain_local_harness import BRAIN_CORE_SRC, sha256_text, write_json  # noqa: E402

from gremlin_town.mcp.local_project_connector import (  # noqa: E402
    CONFIG_SCHEMA,
    ConnectorError,
    LocalProjectConnectorRuntime,
    main,
)

SESSION = "registry-fixture-session"


def vault_manifest(root: Path, exports: str) -> str:
    return f"---\nvault-name: {root.name}\nroot-path: {root.as_posix()}\nexports:\n{exports}intake: inbox/\n---\n"


def build(tmp_path: Path, *, hand_set_id: str = "vault_plans") -> dict[str, Path]:
    """MetaVault (the registry), two vaults with folders, one retired and one missing, and one
    project set by hand in the node map."""

    meta = tmp_path / "meta-vault"
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    retired = tmp_path / "old-site"
    plans = tmp_path / "plans"
    for folder in (meta / "ops", alpha / "architecture", alpha / "ops", beta, retired, plans):
        folder.mkdir(parents=True, exist_ok=True)

    (meta / "ops" / "compass.md").write_text("# MetaVault compass\nFleet focus: registry enrollment.\n", encoding="utf-8")
    (alpha / "compass.md").write_text("# Alpha compass\nAlpha focus: telescopes.\n", encoding="utf-8")
    (alpha / "architecture" / "overview.md").write_text("# Alpha overview\nThe telescope mount design.\n", encoding="utf-8")
    (alpha / "architecture" / ".draft.md").write_text("# Hidden draft\ntelescope draft\n", encoding="utf-8")
    (alpha / "private.md").write_text("# Private\nNot exported: telescope diary.\n", encoding="utf-8")
    (alpha / "ops" / "vault-manifest.md").write_text(
        vault_manifest(alpha, "  compass: compass.md\n  architecture: architecture/\n"), encoding="utf-8"
    )
    (beta / "compass.md").write_text("# Beta compass\nBeta focus: gardens.\n", encoding="utf-8")
    (plans / "plan.md").write_text("# Plan\nHand-set plans folder.\n", encoding="utf-8")

    registry = meta / "ops" / "vault-manifest.md"
    registry.write_text(f"""---
vault-name: MetaVault
root-path: {meta.as_posix()}
exports:
  compass: ops/compass.md
  registry: ops/vault-manifest.md
  tracker: ops/agent-tools/tracker.py # a script, not served
intake: handoffs/
cross-vault-dependencies:

  - vault: {alpha.as_posix()}
    vault-name: Alpha Vault
    compass: compass.md
    loads-from: [compass.md]

  - vault: {beta.as_posix()}
    vault-name: beta
    compass: compass.md
    loads-from: [compass.md, notes/index.md]

  - vault: {retired.as_posix()}
    vault-name: old-site
    compass: compass.md
    retired: 2026-08-07

pending-unpushed:

  - vault: {(tmp_path / 'not-on-this-pc').as_posix()}
    vault-name: elsewhere
    compass: compass.md
---
""", encoding="utf-8")

    write_json(plans / "brain.yaml", {
        "schema": "brain.vault/v1", "id": hand_set_id, "name": "Plans", "purpose": "Hand-set fixture",
        "topics": ["plans"], "sensitivity": "private", "sync_class": "local-only", "default_branch": "main",
        "intent": None, "record_roots": [], "dependencies": [],
        "search": {"paths": ["plan.md"], "purpose": "ranking-only"},
        "exports": [{"name": "plans", "paths": ["plan.md"], "max_bytes": 20000}],
    })
    write_json(plans / ".brain" / "retrieval.json", {
        "schema": "brain.a02-retrieval-catalog/v1", "repository_version": "plans-v1", "links": [],
        "evidence": [{"evidence_id": "E_PLAN", "owner_vault": hand_set_id, "relative_path": "plan.md",
                      "content_sha256": sha256_text("# Plan\nHand-set plans folder.\n"), "state": "current"}],
    })
    node_map = tmp_path / "node-map.json"
    write_json(node_map, {"schema": "brain.node-map/v1", "node": "local",
                          "vaults": [{"id": hand_set_id, "root": plans.as_posix(), "manifest": "brain.yaml"}]})
    hand_set_notes = tmp_path / "plans-notes"
    hand_set_notes.mkdir()
    config = tmp_path / "connector.json"
    write_json(config, {
        "schema": CONFIG_SCHEMA,
        "bind_host": "127.0.0.1",
        "port": 0,
        "encrypted_overlay_networks": [],
        "trusted_client_networks": ["127.0.0.0/8"],
        "auth": [{"token_sha256": sha256_text("registry-token"),
                  "scopes": ["context:read", "continuity:read", "continuity:write"]}],
        "node_map": node_map.as_posix(),
        "projects": [{"id": hand_set_id, "retrieval_catalog": ".brain/retrieval.json", "grants": [],
                      "write_paths": [], "continuity_root": hand_set_notes.as_posix()}],
        "registry": {"path": registry.as_posix(), "state_dir": (tmp_path / "state").as_posix()},
    })
    return {"meta": meta, "alpha": alpha, "beta": beta, "plans": plans, "config": config, "state": tmp_path / "state"}


def files_under(*roots: Path) -> set[Path]:
    return {path for root in roots for path in root.rglob("*")}


def runtime(paths: dict[str, Path]) -> LocalProjectConnectorRuntime:
    return LocalProjectConnectorRuntime(paths["config"], brain_src=BRAIN_CORE_SRC)


def test_every_registered_vault_on_this_pc_is_enrolled_and_nothing_is_written_into_them(tmp_path: Path) -> None:
    paths = build(tmp_path)
    before = files_under(paths["meta"], paths["alpha"], paths["beta"])

    served = runtime(paths).enrollment()

    assert [(item["id"], item["source"]) for item in served["projects"]] == [
        ("vault_alpha_vault", "registry"),
        ("vault_beta", "registry"),
        ("vault_metavault", "registry"),
        ("vault_plans", "config"),
    ]
    by_id = {item["id"]: item for item in served["projects"]}
    assert by_id["vault_alpha_vault"]["exports"] == ["compass.md", "architecture"]
    assert by_id["vault_alpha_vault"]["exports_from"] == "manifest"
    assert by_id["vault_alpha_vault"]["notes"] == (paths["alpha"].resolve() / "ops" / "sessions").as_posix()
    assert by_id["vault_beta"]["exports"] == ["compass.md", "notes/index.md"]
    assert by_id["vault_beta"]["exports_from"] == "registry"
    assert by_id["vault_metavault"]["exports"] == ["ops/compass.md", "ops/vault-manifest.md", "ops/agent-tools/tracker.py"]
    assert [(item["name"], item["reason"]) for item in served["skipped"]] == [
        ("elsewhere", "folder_missing"),
        ("old-site", "retired"),
    ]
    assert files_under(paths["meta"], paths["alpha"], paths["beta"]) == before


def test_agents_read_only_a_vaults_declared_exports(tmp_path: Path) -> None:
    paths = build(tmp_path)
    connector = runtime(paths)
    before = files_under(paths["alpha"])

    result = connector.retrieve_context(
        {"project_id": "vault_alpha_vault", "intent": "telescope", "session_id": SESSION, "max_sources": 10}
    )

    assert sorted(source["relative_path"] for source in result["sources"]) == ["architecture/overview.md"]
    everything = connector.retrieve_context(
        {"project_id": "vault_alpha_vault", "intent": "alpha focus telescope mount diary draft",
         "session_id": SESSION, "max_sources": 10}
    )
    served = {source["relative_path"] for source in everything["sources"]}
    assert served <= {"compass.md", "architecture/overview.md"}
    assert "private.md" not in served and "architecture/.draft.md" not in served
    assert files_under(paths["alpha"]) == before  # the catalog lives in the connector's state folder
    assert (paths["state"] / "catalogs" / "vault_alpha_vault.json").is_file()
    with pytest.raises(ConnectorError):
        connector.read_context({"project_id": "vault_alpha_vault", "relative_path": "private.md", "session_id": SESSION})
    assert "Alpha focus" in connector.read_context(
        {"project_id": "vault_alpha_vault", "relative_path": "compass.md", "session_id": SESSION}
    )["text"]


def test_metavault_serves_its_compass_and_registry_and_never_the_script(tmp_path: Path) -> None:
    paths = build(tmp_path)
    (paths["meta"] / "ops" / "agent-tools").mkdir()
    (paths["meta"] / "ops" / "agent-tools" / "tracker.py").write_text("# registry script\n", encoding="utf-8")
    result = runtime(paths).retrieve_context(
        {"project_id": "vault_metavault", "intent": "registry enrollment fleet", "session_id": SESSION, "max_sources": 10}
    )
    assert {source["relative_path"] for source in result["sources"]} <= {"ops/compass.md", "ops/vault-manifest.md"}
    assert result["sources"]


def test_a_vaults_notes_land_in_its_own_ops_sessions_and_nowhere_else(tmp_path: Path) -> None:
    paths = build(tmp_path)
    connector = runtime(paths)
    notes = paths["alpha"] / "ops" / "sessions"

    assert connector.list_continuity({"project_id": "vault_alpha_vault"}) == {"items": []}
    with pytest.raises(ConnectorError) as empty:
        connector.recover_continuity({"project_id": "vault_alpha_vault"})
    assert empty.value.status == 404
    assert not notes.exists()  # reading makes nothing

    saved = connector.start_continuity({
        "project_id": "vault_alpha_vault", "operation_id": "op-alpha-1", "session_id": "chat-1",
        "intent": "Mount design review", "source_references": [], "next_step": "Check the gear ratios",
    })
    assert saved["state"] == "open"
    written = sorted(path.relative_to(paths["alpha"]).as_posix() for path in paths["alpha"].rglob("*") if path.is_file())
    note_files = [path for path in written if path.startswith("ops/sessions/")]
    assert len(note_files) == 1 and note_files[0].startswith("ops/sessions/records/continuity/")
    assert not (notes / ".connector-operations").exists()
    assert list((paths["state"] / "operations" / "vault_alpha_vault").glob("*.json"))
    assert [item["intent"] for item in connector.list_continuity({"project_id": "vault_alpha_vault"})["items"]] == [
        "Mount design review",
    ]
    assert connector.recover_continuity({"project_id": "vault_alpha_vault"})["next_step"] == "Check the gear ratios"
    # Another vault's notes are its own.
    assert connector.list_continuity({"project_id": "vault_beta"}) == {"items": []}
    assert not (paths["meta"] / "ops" / "sessions").exists()
    # Saving the same operation again answers the same note.
    again = connector.start_continuity({
        "project_id": "vault_alpha_vault", "operation_id": "op-alpha-1", "session_id": "chat-1",
        "intent": "Mount design review", "source_references": [], "next_step": "Check the gear ratios",
    })
    assert again["record_id"] == saved["record_id"]


def test_a_hand_set_project_keeps_its_own_notes_folder(tmp_path: Path) -> None:
    paths = build(tmp_path)
    connector = runtime(paths)
    connector.start_continuity({
        "project_id": "vault_plans", "operation_id": "op-plans-1", "session_id": "chat-2",
        "intent": "Plans pass", "source_references": [], "next_step": "Read the plan",
    })
    assert list((tmp_path / "plans-notes" / "records").rglob("*.md"))
    assert (tmp_path / "plans-notes" / ".connector-operations").is_dir()


def test_an_id_set_by_hand_and_found_in_the_registry_stops_the_start(tmp_path: Path) -> None:
    paths = build(tmp_path, hand_set_id="vault_beta")
    with pytest.raises(ConnectorError) as clash:
        runtime(paths)
    assert clash.value.code == "CONFIGURATION_INVALID" and "vault_beta" in str(clash.value)


@pytest.mark.skipif(os.name != "nt", reason="Windows junction fixture")
def test_a_link_where_the_notes_folder_goes_is_refused(tmp_path: Path) -> None:
    paths = build(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = paths["beta"] / "ops"
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], capture_output=True, text=True)
    if made.returncode != 0:
        pytest.skip(f"could not create a junction: {made.stderr or made.stdout}")
    try:
        with pytest.raises(ConnectorError) as refused:
            runtime(paths).start_continuity({
                "project_id": "vault_beta", "operation_id": "op-beta-1", "session_id": "chat-3",
                "intent": "Garden plan", "source_references": [], "next_step": "Water",
            })
        assert refused.value.code == "REPARSE_REFUSED"
        assert not list(outside.rglob("*"))
    finally:
        link.rmdir()


def test_list_projects_prints_the_enrollment_without_starting_a_server(tmp_path: Path) -> None:
    paths = build(tmp_path)
    printed = io.StringIO()
    with redirect_stdout(printed):
        assert main(["list-projects", "--config", str(paths["config"]), "--brain-src", str(BRAIN_CORE_SRC)]) == 0
    value = json.loads(printed.getvalue())
    assert [item["id"] for item in value["projects"]] == ["vault_alpha_vault", "vault_beta", "vault_metavault", "vault_plans"]
    assert "registry-token" not in printed.getvalue()
