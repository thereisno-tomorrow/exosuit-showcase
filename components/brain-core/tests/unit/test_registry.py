"""MetaVault's registry as the PC connector's enrollment (ROA-72; docs/product/steps/b-notes-in-each-vault.md)."""

from __future__ import annotations

from pathlib import Path

import pytest

from brain_core.discovery import (
    EXPORTS_FROM_COMPASS,
    EXPORTS_FROM_MANIFEST,
    EXPORTS_FROM_REGISTRY,
    REGISTRY_SKIP_FOLDER_MISSING,
    REGISTRY_SKIP_RETIRED,
    RegistryVault,
    discover_legacy_registry,
    read_registry,
    registry_manifest,
    vault_exports,
)
from brain_core.errors import BrainError, ErrorCode


def registry_text(root: Path, *entries: str) -> str:
    """A registry shaped like MetaVault's: its own header, then ``- vault:`` entries in two lists."""

    header = f"""---
vault-name: MetaVault
features: [D]
root-path: {(root / 'meta-vault').as_posix()}
registry-source: "Runtime join: a note with a # inside quotes"
domains:
  - vault-orchestration
exports:
  compass: ops/compass.md
  registry: ops/vault-manifest.md
  delegation-tracker: ops/agent-tools/delegation_tracker.py # callable log-reading utility
intake: handoffs/        # fleet notices
load-instruction:        # legacy alias
  - "Read ops/vault-manifest.md"
cross-vault-dependencies:
"""
    return header + "\n".join(entries) + "\n"


def entry(root: Path, folder: str, name: str, *, extra: str = "", loads_from: str = "[compass.md]") -> str:
    return f"""
  - vault: {(root / folder).as_posix()}
    vault-name: {name}
    slug: 0000abcd
    domains:
      - {folder}-domain
    compass: compass.md
    loads-from: {loads_from}{extra}
"""


def make_vaults(root: Path, *folders: str) -> None:
    for folder in ("meta-vault", *folders):
        (root / folder).mkdir(parents=True, exist_ok=True)


def test_every_registered_vault_whose_folder_exists_is_enrolled_as_vault_and_its_name(tmp_path: Path) -> None:
    make_vaults(tmp_path, "exosuit", "rainmakr-v2", "crypto", "personal-site")
    registry = tmp_path / "meta-vault" / "ops" / "vault-manifest.md"
    registry.parent.mkdir(parents=True)
    registry.write_text(registry_text(
        tmp_path,
        entry(tmp_path, "exosuit", "Exosuit"),
        entry(tmp_path, "rainmakr-v2", "Rainmakr V2"),
        entry(tmp_path, "personal-site", "personal-site", extra="\n    retired: 2026-08-07\n    superseded-by: x"),
        entry(tmp_path, "gone", "Gone Vault"),
        "\npending-unpushed:\n",
        entry(tmp_path, "crypto", "crypto"),
    ), encoding="utf-8")

    vaults, skipped = read_registry(registry)

    assert [vault.id for vault in vaults] == ["vault_crypto", "vault_exosuit", "vault_metavault", "vault_rainmakr_v2"]
    by_id = {vault.id: vault for vault in vaults}
    metavault = by_id["vault_metavault"]
    assert metavault.name == "MetaVault" and metavault.compass == "ops/compass.md"
    assert metavault.loads_from == (
        "ops/compass.md", "ops/vault-manifest.md", "ops/agent-tools/delegation_tracker.py",
    )
    assert Path(metavault.root) == (tmp_path / "meta-vault").resolve()
    assert by_id["vault_exosuit"].loads_from == ("compass.md",)
    assert by_id["vault_exosuit"].topics == ("exosuit-domain",)
    assert [(skip.name, skip.reason) for skip in skipped] == [
        ("Gone Vault", REGISTRY_SKIP_FOLDER_MISSING),
        ("personal-site", REGISTRY_SKIP_RETIRED),
    ]


def test_two_vaults_with_one_id_fail_loudly(tmp_path: Path) -> None:
    make_vaults(tmp_path, "one", "two")
    registry = tmp_path / "registry.md"
    registry.write_text(registry_text(
        tmp_path, entry(tmp_path, "one", "Same Name"), entry(tmp_path, "two", "same-name"),
    ), encoding="utf-8")
    with pytest.raises(BrainError) as raised:
        read_registry(registry)
    assert raised.value.code is ErrorCode.DUPLICATE_ID


def test_a_registry_without_its_own_header_is_refused(tmp_path: Path) -> None:
    registry = tmp_path / "registry.md"
    registry.write_text("cross-vault-dependencies:\n" + entry(tmp_path, "one", "One"), encoding="utf-8")
    with pytest.raises(BrainError) as raised:
        read_registry(registry)
    assert raised.value.code is ErrorCode.CONTRACT_INVALID


def vault(root: Path, *, loads_from: tuple[str, ...] = ("compass.md",)) -> RegistryVault:
    root.mkdir(parents=True, exist_ok=True)
    return RegistryVault(id="vault_sample", name="Sample", root=str(root.resolve()), compass="compass.md",
                         loads_from=loads_from)


def write_manifest(root: Path, body: str, *, root_path: Path | None = None) -> None:
    manifest = root / "ops" / "vault-manifest.md"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        f"---\nvault-name: Sample\nroot-path: {(root_path or root).as_posix()}\n{body}---\n\n# Sample\n",
        encoding="utf-8",
    )


def test_a_vaults_own_manifest_exports_come_first(tmp_path: Path) -> None:
    root = tmp_path / "sample"
    sample = vault(root)
    write_manifest(root, """exports:
  compass: compass.md   # the default export
  architecture: architecture/
  more: [notes/index.md, research/]
  surfaces:
    - context/brief.md
intake: inbox/
""")
    found = vault_exports(sample)
    assert found.source == EXPORTS_FROM_MANIFEST
    assert found.paths == ("compass.md", "architecture", "notes/index.md", "research", "context/brief.md")
    assert found.ignored == ()


def test_a_manifest_for_another_folder_is_not_trusted_and_the_registry_is_used(tmp_path: Path) -> None:
    root = tmp_path / "sample"
    sample = vault(root, loads_from=("compass.md", "notes/index.md"))
    write_manifest(root, "exports:\n  compass: compass.md\n  everything: .\n", root_path=tmp_path / "elsewhere")
    found = vault_exports(sample)
    assert found.source == EXPORTS_FROM_REGISTRY
    assert found.paths == ("compass.md", "notes/index.md")


def test_with_no_manifest_and_no_loads_from_only_the_compass_is_readable(tmp_path: Path) -> None:
    found = vault_exports(vault(tmp_path / "sample", loads_from=()))
    assert (found.source, found.paths) == (EXPORTS_FROM_COMPASS, ("compass.md",))


def test_an_unsafe_declared_path_is_left_out_and_named(tmp_path: Path) -> None:
    root = tmp_path / "sample"
    sample = vault(root)
    write_manifest(root, "exports:\n  compass: compass.md\n  escape: ../other-vault/secrets.md\n")
    found = vault_exports(sample)
    assert found.paths == ("compass.md",)
    assert found.ignored == ("../other-vault/secrets.md",)


def test_the_connectors_manifest_exports_exactly_the_paths_given(tmp_path: Path) -> None:
    sample = vault(tmp_path / "sample")
    manifest = registry_manifest(sample, ("compass.md", "notes/index.md"))
    assert manifest.id == "vault_sample" and manifest.root == sample.root
    assert manifest.exports[0].paths == ("compass.md", "notes/index.md")
    assert manifest.search.paths == ("compass.md", "notes/index.md")
    assert manifest.intent == "compass.md"


def test_the_legacy_registry_reader_still_reads_the_same_entries(tmp_path: Path) -> None:
    make_vaults(tmp_path, "exosuit")
    registry = tmp_path / "registry.md"
    registry.write_text(registry_text(tmp_path, entry(tmp_path, "exosuit", "Exosuit")), encoding="utf-8")
    node_map, manifests = discover_legacy_registry(registry)
    assert [item.id for item in node_map.vaults] == ["vault_exosuit"]
    assert manifests[0].exports[0].paths == ("compass.md",)
