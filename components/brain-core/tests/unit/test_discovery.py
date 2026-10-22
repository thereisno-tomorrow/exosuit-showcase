from __future__ import annotations

from pathlib import Path

import pytest

from brain_core.discovery import discover_vaults, load_node_map
from brain_core.errors import BrainError, ErrorCode


def _manifest(vault_id: str, *, paths: tuple[str, ...] = ("docs/source.md",)) -> str:
    rendered = "\n".join(f"      - {path}" for path in paths)
    return f"""schema: brain.vault/v1
id: {vault_id}
name: {vault_id}
purpose: synthetic fixture
exports:
  - name: evidence
    paths:
{rendered}
"""


def _vault(root: Path, vault_id: str, *, manifest_id: str | None = None) -> None:
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "source.md").write_text(f"source for {vault_id}", encoding="utf-8")
    (root / "brain.yaml").write_text(_manifest(manifest_id or vault_id), encoding="utf-8")


def _node_map(path: Path, entries: list[tuple[str, Path]], *, manifest: str = "brain.yaml") -> None:
    lines = ["schema: brain.node-map/v1", "node: local", "vaults:"]
    for vault_id, root in entries:
        lines.extend((f"  - id: {vault_id}", f'    root: "{root.as_posix()}"', f"    manifest: {manifest}"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_discovery_is_deterministic_and_normalizes_manifest(tmp_path: Path) -> None:
    second = tmp_path / "second"
    first = tmp_path / "first"
    _vault(second, "vault_second")
    _vault(first, "vault_first")
    node_map = tmp_path / "node.yaml"
    _node_map(node_map, [("vault_second", second), ("vault_first", first)], manifest=".\\brain.yaml")

    loaded = load_node_map(node_map)
    manifests = discover_vaults(node_map)

    assert [entry.id for entry in loaded.vaults] == ["vault_first", "vault_second"]
    assert {entry.manifest for entry in loaded.vaults} == {"brain.yaml"}
    assert [manifest.id for manifest in manifests] == ["vault_first", "vault_second"]
    assert all(Path(manifest.root).is_absolute() for manifest in manifests)


def test_duplicate_enrollment_id_reports_both_roots(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    node_map = tmp_path / "node.yaml"
    _node_map(node_map, [("vault_same", first), ("VAULT_SAME", second)])

    with pytest.raises(BrainError) as raised:
        load_node_map(node_map)

    assert raised.value.code is ErrorCode.DUPLICATE_ID
    assert raised.value.details["first_root"] == first.as_posix()
    assert raised.value.details["second_root"] == second.as_posix()


def test_duplicate_or_case_colliding_roots_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "Repository"
    _vault(root, "vault_first")
    node_map = tmp_path / "node.yaml"
    _node_map(node_map, [("vault_first", root), ("vault_second", root)])

    with pytest.raises(BrainError) as raised:
        discover_vaults(node_map)

    assert raised.value.code is ErrorCode.PATH_CASE_COLLISION
    assert raised.value.details["first_id"] == "vault_first"
    assert raised.value.details["second_id"] == "vault_second"


def test_case_variant_root_spellings_are_refused(tmp_path: Path) -> None:
    first = tmp_path / "CaseRoot"
    second = tmp_path / "caseroot"
    _vault(first, "vault_first")
    if not second.is_dir():
        _vault(second, "vault_second")
    node_map = tmp_path / "node.yaml"
    _node_map(node_map, [("vault_first", first), ("vault_second", second)])

    with pytest.raises(BrainError) as raised:
        discover_vaults(node_map)

    assert raised.value.code is ErrorCode.PATH_CASE_COLLISION
    assert raised.value.details["first_root"].casefold() == raised.value.details["second_root"].casefold()


def test_missing_root_fails_before_manifest_read(tmp_path: Path) -> None:
    node_map = tmp_path / "node.yaml"
    _node_map(node_map, [("vault_missing", tmp_path / "missing")])

    with pytest.raises(BrainError) as raised:
        discover_vaults(node_map)

    assert raised.value.code is ErrorCode.ROOT_MISSING


def test_duplicate_manifest_ids_and_enrollment_mismatch_are_visible(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _vault(first, "vault_first", manifest_id="vault_duplicate")
    _vault(second, "vault_second", manifest_id="vault_duplicate")
    node_map = tmp_path / "node.yaml"
    _node_map(node_map, [("vault_first", first), ("vault_second", second)])

    with pytest.raises(BrainError) as raised:
        discover_vaults(node_map)
    assert raised.value.code is ErrorCode.DUPLICATE_ID

    (second / "brain.yaml").write_text(_manifest("vault_second"), encoding="utf-8")
    with pytest.raises(BrainError) as raised:
        discover_vaults(node_map)
    assert raised.value.code is ErrorCode.CONTRACT_INVALID
    assert raised.value.details["enrolled_id"] == "vault_first"


def test_strict_yaml_and_declared_path_collision(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _vault(root, "vault_fixture")
    node_map = tmp_path / "node.yaml"
    _node_map(node_map, [("vault_fixture", root)])
    (root / "brain.yaml").write_text(
        _manifest("vault_fixture").replace("name: vault_fixture", "name: one\nname: two"),
        encoding="utf-8",
    )
    with pytest.raises(BrainError) as raised:
        discover_vaults(node_map)
    assert raised.value.code is ErrorCode.YAML_INVALID

    (root / "brain.yaml").write_text(
        _manifest("vault_fixture", paths=("docs/source.md", "DOCS/source.md")),
        encoding="utf-8",
    )
    with pytest.raises(BrainError) as raised:
        discover_vaults(node_map)
    assert raised.value.code is ErrorCode.PATH_CASE_COLLISION


def test_manifest_path_must_be_repository_relative(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    root.mkdir()
    node_map = tmp_path / "node.yaml"
    _node_map(node_map, [("vault_fixture", root)], manifest="../brain.yaml")

    with pytest.raises(BrainError) as raised:
        load_node_map(node_map)

    assert raised.value.code is ErrorCode.PATH_TRAVERSAL
