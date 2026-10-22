from pathlib import Path
import hashlib
import json
import os
import socket
import pytest
from gremlin_town.host_packs.loader import dry_load_host_packs
from gremlin_town.mcp.local_context_catalog import scan_markdown_folder, FolderCatalogLimits

ROOT = Path(__file__).resolve().parents[2] / "host_packs"

@pytest.fixture
def directory_reparse():
    links = []

    def create(link, target):
        if os.name == "nt":
            # Junctions require no Windows symlink privilege.
            import _winapi
            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
        links.append(link)

    yield create

    for link in reversed(links):
        if link.is_symlink():
            link.unlink()
        elif link.is_junction():
            link.rmdir()

def test_dry_load_is_offline_and_never_native(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("network forbidden")
    monkeypatch.setattr(socket, "socket", denied)
    report = dry_load_host_packs(ROOT)
    assert len(report["sha256"]) == 7
    assert "grok/GATEWAY.md" in report["sha256"]
    assert report["native_loaded"] is False and report["installed"] is False
    assert report["auto_activate"] is False
    assert report["sha256"]["shared/CONTRACT.md"] == hashlib.sha256((ROOT / "shared/CONTRACT.md").read_bytes()).hexdigest()

@pytest.mark.parametrize("relative", ["../outside.md", "/outside.md", "C:/outside.md", "..\\outside.md", "file.md:stream", "missing.md"])
def test_dry_load_refuses_untrusted_paths(tmp_path, relative):
    (tmp_path / "pack.manifest.json").write_text(json.dumps({"schema":"gt.host-preparation/v1", "status":"candidate", "markdown_paths":[relative]}))
    with pytest.raises((ValueError, FileNotFoundError)):
        dry_load_host_packs(tmp_path)

@pytest.mark.parametrize("status,paths", [("active", ["body.md"]), ("candidate", ["body.md"]*2), ("candidate", ["body.json"]), ("candidate", [])])
def test_invalid_manifest_fails_loudly(tmp_path, status, paths):
    (tmp_path / "pack.manifest.json").write_text(json.dumps({"schema":"gt.host-preparation/v1", "status":status, "markdown_paths":paths}))
    with pytest.raises(ValueError):
        dry_load_host_packs(tmp_path)

def test_manifest_cannot_follow_reparse(tmp_path, directory_reparse):
    external = tmp_path / "external"
    external.mkdir()
    (external / "body.md").write_text("fixture outside pack")
    pack = tmp_path / "pack"
    pack.mkdir()
    link = pack / "escape"
    directory_reparse(link, external)
    (pack / "pack.manifest.json").write_text(json.dumps({"schema":"gt.host-preparation/v1", "status":"candidate", "markdown_paths":["escape/body.md"]}))
    with pytest.raises(ValueError, match="reparse"):
        dry_load_host_packs(pack)

def test_plans_edit_new_file_hash_and_bounded_enumeration(tmp_path):
    relative = "docs/plans/gremlin-town-bc"
    folder = tmp_path / relative
    folder.mkdir(parents=True)
    original = folder / "edit.md"
    original.write_bytes(b"GTBC-WB-PROOF revision one\n")
    def scan(**kwargs):
        return scan_markdown_folder(vault_root=tmp_path, scan_root=relative, owner_vault="vault_gtbc_plans", **kwargs)
    first = scan()
    original.write_bytes(b"GTBC-WB-PROOF revision two\n")
    (folder / "new.md").write_bytes(b"GTBC-WB-PROOF new document\n")
    second = scan()
    assert len(first.path_hashes) == 1 and len(second.path_hashes) == 2
    assert first.catalog["repository_version"] != second.catalog["repository_version"]
    assert dict(second.path_hashes)[relative + "/edit.md"] == hashlib.sha256(original.read_bytes()).hexdigest()
    assert all(e["owner_vault"] == "vault_gtbc_plans" for e in second.catalog["evidence"])
    bounded = scan(limits=FolderCatalogLimits(max_enumerated_entries=1))
    assert bounded.report.limited
    assert "ENUM_LIMIT" in {e["code"] for e in bounded.report.exclusions}
    with pytest.raises(ValueError):
        scan_markdown_folder(vault_root=tmp_path, scan_root="../outside", owner_vault="vault_gtbc_plans")

def test_catalog_excludes_junction_escape(tmp_path, directory_reparse):
    vault = tmp_path / "vault"
    folder = vault / "docs/plans/gremlin-town-bc"
    folder.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "not-enrolled.md").write_text("synthetic denial canary")
    link = folder / "escape"
    directory_reparse(link, outside)
    result = scan_markdown_folder(vault_root=vault, scan_root="docs/plans/gremlin-town-bc", owner_vault="vault_gtbc_plans")
    assert not result.path_hashes
    assert "ESCAPE_EXCLUDED" in {e["code"] for e in result.report.exclusions}
