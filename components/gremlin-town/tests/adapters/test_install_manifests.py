from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[2]


def load_manifest(relative_path: str) -> dict:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def git_blob_bytes(path: Path) -> bytes:
    """The committed bytes for `path` at HEAD, regardless of the local checkout's
    line-ending conversion (core.autocrlf). This is what `git archive` ships."""
    repo_root = Path(
        subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path.parent,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    relative = path.resolve().relative_to(repo_root.resolve()).as_posix()
    return subprocess.run(
        ["git", "show", f"HEAD:{relative}"],
        cwd=repo_root,
        capture_output=True,
        check=True,
    ).stdout


def git_blob_sha256(path: Path) -> str:
    return hashlib.sha256(git_blob_bytes(path)).hexdigest()


def _assert_pin_is_git_blob_sha256(root: Path, entry: dict) -> None:
    actual = git_blob_sha256(root / entry["source"])
    assert actual == entry["sha256"], (
        f"{entry['source']}: pin {entry['sha256']} is not sha256 of the committed git blob bytes "
        f"(got {actual}); a working-tree/CRLF hash would not survive a `git archive` release build"
    )


def test_cloud_install_manifest_builds_complete_standalone_launcher_tree(tmp_path: Path) -> None:
    manifest = load_manifest("adapters/cloud/install-manifest.json")
    assert manifest["install_root"] == "/home/box/gremlin-town-client"
    assert manifest["hub_runtime_reference"] == "/opt/gremlin-town/current"
    assert manifest["runtime_config"]["default_token_file"] == "/home/box/.config/gremlin-town/grok.token"
    assert manifest["runtime_config"]["credential_scope"] == "shared-cloud-installation-not-bot-isolation"

    install_root = tmp_path / "gremlin-town-client"
    expected_destinations = {
        "/home/box/gremlin-town-client/gremlin_town_client/__init__.py",
        "/home/box/gremlin-town-client/gremlin_town_client/__main__.py",
        "/home/box/gremlin-town-client/gremlin_town_client/client.py",
        "/home/box/gremlin-town-client/gremlin_town_client/hub_client.py",
        "/home/box/gremlin-town-client/gremlin_town_client/client-settings.json",
        "/home/box/gremlin-town-client/bin/gremlin-town-client",
    }
    assert {entry["destination"] for entry in manifest["files"]} == expected_destinations
    for entry in manifest["files"]:
        source = ROOT / entry["source"]
        assert source.is_file()
        relative_destination = Path(entry["destination"]).relative_to("/home/box/gremlin-town-client")
        destination = install_root / relative_destination
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    launcher = install_root / "bin" / "gremlin-town-client"
    environment = dict(os.environ)
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, str(launcher), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "participants.list" in result.stdout
    assert "shared-cloud installation" in " ".join(result.stdout.split())
    assert "adapters.hermes" not in (install_root / "gremlin_town_client" / "client.py").read_text(encoding="utf-8")


def test_hermes_install_manifest_lists_exact_standalone_plugin_tree() -> None:
    manifest = load_manifest("adapters/hermes/install-manifest.json")
    assert manifest["install_root"] == "/opt/data/plugins/gremlin-town"
    assert manifest["host_backing_root"] == "/srv/hermes-example/plugins/gremlin-town"
    assert manifest["hub_runtime_reference"] == "/opt/gremlin-town/current"
    assert [Path(entry["destination"]).name for entry in manifest["files"]] == [
        "__init__.py",
        "plugin.py",
        "hub_client.py",
        "plugin.yaml",
        "settings.json",
        "plugin-settings.json",
    ]
    assert manifest["runtime_config"]["accepted_token_sources"] == [
        "GREMLIN_TOWN_HUB_TOKEN",
        "GREMLIN_TOWN_HUB_TOKEN_FILE",
    ]
    assert all((ROOT / entry["source"]).is_file() for entry in manifest["files"])


def test_hermes_skill_package_has_verified_separate_root(tmp_path: Path) -> None:
    from pathlib import PurePosixPath

    manifest = load_manifest("adapters/hermes/install-manifest.json")
    root = PurePosixPath(manifest["skill_install_root"])
    assert str(root) == "/opt/data/skills"
    assert manifest["skill_host_backing_root"] == "/srv/hermes-example/skills"
    destinations = set()
    for entry in manifest["skill_files"]:
        relative = PurePosixPath(entry["destination"]).relative_to(root)
        assert ".." not in relative.parts
        assert str(relative) not in destinations
        destinations.add(str(relative))
        source = ROOT / entry["source"]
        assert git_blob_sha256(source) == entry["sha256"]
        raw = source.read_bytes()
        destination = tmp_path.joinpath(*relative.parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(raw)
        assert destination.read_bytes() == raw
    assert destinations == {"gremlin-town/SKILL.md", "gremlin-town-review/SKILL.md", "next-action/SKILL.md", "calendar-time/SKILL.md"}
    for name in ("gremlin-town", "gremlin-town-review", "next-action"):
        text = (tmp_path / name / "SKILL.md").read_text(encoding="utf-8").replace("\r\n", "\n")
        assert text.startswith(f"---\nname: {name}\ndescription: ")
        assert "\n---\n" in text
        assert not any(x in text for x in ("C:\\Users", "gt-staged-candidate", "req_a2ad"))


def test_hermes_install_manifest_pin_check_rejects_a_crlf_hashed_value_and_accepts_the_corrected_manifest() -> None:
    """Regression guard for the CRLF-vs-LF pin bug: a pin hashed from CRLF working-copy
    bytes must be rejected, and the manifest's actual (corrected) pins must be accepted."""
    manifest = load_manifest("adapters/hermes/install-manifest.json")
    entry = next(e for e in manifest["files"] if Path(e["source"]).name == "plugin.py")

    committed_bytes = git_blob_bytes(ROOT / entry["source"])
    crlf_bytes = committed_bytes.replace(b"\n", b"\r\n")
    assert crlf_bytes != committed_bytes  # sanity: this file has line endings to convert

    crlf_hashed_entry = dict(entry, sha256=hashlib.sha256(crlf_bytes).hexdigest())
    assert crlf_hashed_entry["sha256"] != entry["sha256"]

    with pytest.raises(AssertionError):
        _assert_pin_is_git_blob_sha256(ROOT, crlf_hashed_entry)

    for real_entry in manifest["files"] + manifest["skill_files"]:
        _assert_pin_is_git_blob_sha256(ROOT, real_entry)


def test_next_action_packaged_skill_matches_its_committed_source_pin() -> None:
    """The public adapter skill is authoritative; private agent controls are excluded."""
    manifest = load_manifest("adapters/hermes/install-manifest.json")
    entry = next(entry for entry in manifest["skill_files"]
                 if entry["destination"] == "/opt/data/skills/next-action/SKILL.md")
    assert entry["source"] == "adapters/hermes/skills/next-action/SKILL.md"
    _assert_pin_is_git_blob_sha256(ROOT, entry)
