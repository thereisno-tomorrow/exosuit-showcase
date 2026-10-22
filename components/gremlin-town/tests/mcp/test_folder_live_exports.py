import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
from brain_local_harness import build_fixture, SESSION_ID, write_json
from gremlin_town.mcp.local_project_connector import LocalProjectConnectorRuntime


def test_enrolled_folder_discovers_new_and_edited_files_without_other_roots(tmp_path):
    fixture = build_fixture(tmp_path)
    folder = fixture.git_project / "plans"
    folder.mkdir()
    first = folder / "first.md"
    first.write_text("# Needleproof\nNeedleproof initial revision", encoding="utf-8")
    config = json.loads(fixture.config_path.read_text())
    project = next(p for p in config["projects"] if p["id"] == fixture.git_project_id)
    project["folder_catalog"] = {"enabled": True, "scan_root": "plans", "export_scanned_files": True}
    write_json(fixture.config_path, config)
    runtime = LocalProjectConnectorRuntime(fixture.config_path)
    query = {"project_id": fixture.git_project_id, "intent": "Needleproof", "session_id": SESSION_ID}
    initial = runtime.retrieve_context(query)
    assert len(initial["sources"]) == 1
    old_hash = initial["sources"][0]["content_sha256"]
    first.write_text("# Needleproof\nNeedleproof edited revision", encoding="utf-8")
    (folder / "new.md").write_text("# Needleproof\nNeedleproof new file", encoding="utf-8")
    current = runtime.retrieve_context(query)
    assert len(current["sources"]) == 2
    assert old_hash not in {s["content_sha256"] for s in current["sources"]}
    assert current["repository_version"] != initial["repository_version"]
    assert "LOCAL-DENIED-CANARY" not in json.dumps(current)
    assert not runtime.retrieve_context(dict(query, intent="telemetry"))["sources"]


def test_scan_alone_does_not_expand_exports(tmp_path):
    fixture = build_fixture(tmp_path)
    (fixture.git_project / "new.md").write_text("# Needleproof\nNeedleproof", encoding="utf-8")
    config = json.loads(fixture.config_path.read_text())
    project = next(p for p in config["projects"] if p["id"] == fixture.git_project_id)
    project["folder_catalog"] = {"enabled": True, "scan_root": "."}
    write_json(fixture.config_path, config)
    runtime = LocalProjectConnectorRuntime(fixture.config_path)
    result = runtime.retrieve_context({"project_id": fixture.git_project_id, "intent": "Needleproof", "session_id": SESSION_ID})
    assert not result["sources"]


@pytest.mark.parametrize("scan_root,target_outside_vault", [
    ("plans", False), ("plans/nested", False), ("plans", True),
])
def test_scan_root_junction_denied_before_reads_resolve_or_catalog_write(
    tmp_path, monkeypatch, scan_root, target_outside_vault,
):
    from gremlin_town.mcp import local_context_catalog as catalog
    from gremlin_town.mcp.local_project_connector import ConnectorError

    fixture = build_fixture(tmp_path)
    target = (tmp_path if target_outside_vault else fixture.git_project) / "outside-plans"
    (target / "nested").mkdir(parents=True)
    (target / "canary.md").write_text("# Boundaryneedle\nBoundaryneedle outside enrollment")
    (target / "nested" / "canary.md").write_text("# Boundaryneedle\nBoundaryneedle outside enrollment")
    link = fixture.git_project / "plans"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)
    try:
        config = json.loads(fixture.config_path.read_text())
        project = next(p for p in config["projects"] if p["id"] == fixture.git_project_id)
        project["folder_catalog"] = {"enabled": True, "scan_root": scan_root, "export_scanned_files": True}
        write_json(fixture.config_path, config)
        runtime = LocalProjectConnectorRuntime(fixture.config_path)
        effects = {"read": 0, "publish": 0, "resolve": 0}
        original_read, original_resolve = Path.read_bytes, Path.resolve
        def read(path):
            effects["read"] += 1
            return original_read(path)
        def publish(*args, **kwargs):
            effects["publish"] += 1
            raise AssertionError("denied scan cannot publish a catalog")
        def resolve(path, *args, **kwargs):
            effects["resolve"] += 1
            return original_resolve(path, *args, **kwargs)
        with monkeypatch.context() as patched:
            patched.setattr(Path, "read_bytes", read)
            patched.setattr(Path, "resolve", resolve)
            patched.setattr(catalog, "publish_catalog_atomic", publish)
            with pytest.raises(ConnectorError, match="reparse"):
                runtime.retrieve_context({"project_id": fixture.git_project_id,
                    "intent": "Boundaryneedle", "session_id": SESSION_ID})
        assert effects == {"read": 0, "publish": 0, "resolve": 0}
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_nested_junction_never_enters_scanned_export_set(tmp_path, monkeypatch):
    from gremlin_town.mcp.local_context_catalog import scan_markdown_folder
    vault = tmp_path / "vault"
    plans = vault / "plans"
    outside = vault / "other"
    plans.mkdir(parents=True)
    outside.mkdir()
    (plans / "allowed.md").write_text("Allowed")
    (outside / "canary.md").write_text("Outside enrollment")
    link = plans / "shortcut"
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(outside), str(link))
    else:
        link.symlink_to(outside, target_is_directory=True)
    try:
        reads = []
        original = Path.read_bytes
        def read(path):
            reads.append(path.relative_to(vault).as_posix())
            return original(path)
        monkeypatch.setattr(Path, "read_bytes", read)
        result = scan_markdown_folder(vault_root=vault, scan_root="plans", owner_vault="fixture")
        assert reads == ["plans/allowed.md"]
        assert [path for path, _ in result.path_hashes] == ["plans/allowed.md"]
    finally:
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()
