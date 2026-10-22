from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

from brain_core.operations import dispatch
from brain_core.protocol import ProtocolRequest


SOURCE_FIXTURE = Path(__file__).parents[1] / "fixtures" / "foundation"
NOW = "2026-08-29T00:00:00Z"
SESSION = "synthetic-pilot-session"
WITHHELD = {
    ("vault_career", "profile/career-record.md"),
    ("vault_career", "profile/expertise.md"),
    ("vault_career", "strategy/positioning.md"),
    ("vault_career", "materials/resumes/base-pmm.md"),
    ("vault_augmentus", "reference/jd-augmentus-marketing.md"),
}


def tree_sha256(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def prepare_fixture(tmp_path: Path) -> tuple[Path, list[str], Path]:
    fixture = tmp_path / "foundation"
    shutil.copytree(SOURCE_FIXTURE, fixture)
    node_map = fixture / "node-map-runtime.yaml"
    node_map.write_text(
        "schema: brain.node-map/v1\n"
        "node: synthetic-local\n"
        "vaults:\n"
        "  - id: vault_career\n"
        f"    root: '{(fixture / 'vaults' / 'career').as_posix()}'\n"
        "    manifest: brain.yaml\n"
        "  - id: vault_augmentus\n"
        f"    root: '{(fixture / 'vaults' / 'augmentus').as_posix()}'\n"
        "    manifest: brain.yaml\n",
        encoding="utf-8",
    )
    grants = [
        str(fixture / "grants" / "exact-five-paths" / "career.yaml"),
        str(fixture / "grants" / "exact-five-paths" / "augmentus.yaml"),
    ]
    return node_map, grants, fixture


def call(operation: str, request_id: str = "pilot-request", **parameters: object) -> dict:
    return dispatch(ProtocolRequest("1", request_id, operation, dict(parameters)))


def context_parameters(node_map: Path, database: Path, grants: list[str]) -> dict[str, object]:
    return {
        "node_map": str(node_map),
        "database": str(database),
        "grant_files": grants,
        "session_id": SESSION,
        "intent": "Use Career evidence for honest Augmentus interview positioning and gaps",
        "vaults": ["vault_career", "vault_augmentus"],
        "facets": ["career", "augmentus", "evidence", "gaps"],
        "now": NOW,
        "source_tokens": 12_000,
        "vault_budget": 4,
        "excerpt_budget": 12,
    }


def test_two_stage_read_only_pilot_is_private_bounded_and_reproducible(tmp_path: Path) -> None:
    source_before = tree_sha256(SOURCE_FIXTURE)
    node_map, grants, fixture = prepare_fixture(tmp_path)
    fixture_before = tree_sha256(fixture)
    database = tmp_path / "state" / "brain.db"

    ungranted_build = call(
        "index.rebuild",
        node_map=str(node_map),
        database=str(database),
        grant_files=[],
        session_id=SESSION,
        now=NOW,
    )
    assert ungranted_build["files"] == 2
    ungranted = call(
        "context.build",
        **context_parameters(node_map, database, []),
    )
    assert ungranted["result"] == "evidence-insufficient"
    assert set(ungranted["selected_vaults"]) == {"vault_career", "vault_augmentus"}
    disclosed = {(item["vault"], item["path"]) for item in ungranted["sources"]}
    excluded_paths = {(item["vault"], item["path"]) for item in ungranted["excluded"]}
    assert not disclosed & WITHHELD
    assert not excluded_paths & WITHHELD
    assert all(item["path"] is None for item in ungranted["excluded"])

    first_build = call(
        "index.rebuild",
        node_map=str(node_map),
        database=str(database),
        grant_files=grants,
        session_id=SESSION,
        now=NOW,
    )
    first = call("context.build", **context_parameters(node_map, database, grants))
    assert first["result"] == "ok"
    assert set(first["selected_vaults"]) == {"vault_career", "vault_augmentus"}
    granted_sources = {(item["vault"], item["path"]) for item in first["sources"]}
    assert WITHHELD <= granted_sources
    assert sum(item["source_tokens"] for item in first["sources"]) <= 12_000
    assert len(first["sources"]) <= 12
    assert all(
        item["grant_sha256"] is not None
        for item in first["sources"]
        if (item["vault"], item["path"]) in WITHHELD
    )

    database.unlink()
    database.with_name(f"{database.name}.status.json").unlink()
    second_build = call(
        "index.rebuild",
        node_map=str(node_map),
        database=str(database),
        grant_files=grants,
        session_id=SESSION,
        now=NOW,
    )
    second = call("context.build", **context_parameters(node_map, database, grants))
    assert second_build["generation"] == first_build["generation"]
    assert second["pack_id"] == first["pack_id"]
    assert second["pack_sha256"] == first["pack_sha256"]
    assert second["policy_sha256"] == first["policy_sha256"]
    assert tree_sha256(fixture) == fixture_before
    assert tree_sha256(SOURCE_FIXTURE) == source_before


def test_human_argument_and_json_protocol_paths_produce_identical_pack_hashes(tmp_path: Path) -> None:
    node_map, grants, _ = prepare_fixture(tmp_path)
    database = tmp_path / "brain.db"
    call(
        "index.rebuild",
        node_map=str(node_map),
        database=str(database),
        grant_files=grants,
        session_id=SESSION,
        now=NOW,
    )
    common = [
        "--node-map", str(node_map),
        "--database", str(database),
        "--intent", "Use Career evidence for honest Augmentus interview positioning and gaps",
        "--vault", "vault_career",
        "--vault", "vault_augmentus",
        "--facet", "career",
        "--facet", "augmentus",
        "--facet", "evidence",
        "--facet", "gaps",
        "--session-id", SESSION,
        "--now", NOW,
        "--request-id", "parity-request",
        "--json",
    ]
    for grant in grants:
        common.extend(("--grant-file", grant))
    argument_result = subprocess.run(
        [sys.executable, "-m", "brain_core", "context", "build", *common],
        check=True,
        capture_output=True,
        text=True,
    )
    argument_envelope = json.loads(argument_result.stdout)
    protocol_request = {
        "protocol_version": "1",
        "request_id": "parity-request",
        "operation": "context.build",
        "parameters": context_parameters(node_map, database, grants),
    }
    json_result = subprocess.run(
        [sys.executable, "-m", "brain_core", "json"],
        input=json.dumps(protocol_request),
        check=True,
        capture_output=True,
        text=True,
    )
    json_envelope = json.loads(json_result.stdout)
    assert argument_envelope["data"]["pack_sha256"] == json_envelope["data"]["pack_sha256"]
    assert argument_envelope["data"]["policy_sha256"] == json_envelope["data"]["policy_sha256"]
    assert argument_envelope["data"]["sources"] == json_envelope["data"]["sources"]
