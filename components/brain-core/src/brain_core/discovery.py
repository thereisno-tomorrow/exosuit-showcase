"""Deterministic node-map and enrolled-vault discovery."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from brain_core.contracts import (
    NodeMap,
    NodeEntry,
    ExportSpec,
    SearchSpec,
    VaultManifest,
    node_map_from_mapping,
    parse_yaml,
    vault_manifest_from_mapping,
)
from brain_core.errors import BrainError, ErrorCode
from brain_core.paths import (
    assert_no_case_collisions,
    normalize_relative,
    read_local_text,
    read_snapshot,
    validate_root,
)


def _duplicate_id(entries: tuple[tuple[str, str], ...], *, locator: str) -> None:
    seen: dict[str, tuple[str, str]] = {}
    for vault_id, root in entries:
        key = vault_id.casefold()
        previous = seen.get(key)
        if previous is not None:
            raise BrainError(
                ErrorCode.DUPLICATE_ID,
                f"Duplicate vault ID in {locator}: {vault_id}",
                details={
                    "locator": locator,
                    "first_id": previous[0],
                    "first_root": previous[1],
                    "second_id": vault_id,
                    "second_root": root,
                },
                hint="Give every enrolled repository one stable, unique vault ID.",
            )
        seen[key] = (vault_id, root)


def load_node_map(path: str | Path) -> NodeMap:
    """Load and normalize the machine-local enrollment map."""

    locator = str(Path(path))
    mapping = parse_yaml(read_local_text(path), locator)
    node_map = node_map_from_mapping(mapping, locator)
    _duplicate_id(tuple((entry.id, entry.root) for entry in node_map.vaults), locator=locator)

    normalized_entries = []
    for entry in node_map.vaults:
        manifest = normalize_relative(entry.manifest)
        normalized_entries.append(type(entry)(id=entry.id, root=entry.root, manifest=manifest))
    return NodeMap(
        schema=node_map.schema,
        node=node_map.node,
        vaults=tuple(sorted(normalized_entries, key=lambda entry: (entry.id.casefold(), entry.root.casefold()))),
    )


def _validate_roots(node_map: NodeMap, locator: str) -> dict[str, Path]:
    roots: dict[str, Path] = {}
    seen: dict[str, tuple[str, str]] = {}
    for entry in node_map.vaults:
        resolved = validate_root(entry.root)
        rendered = str(resolved).replace("\\", "/").rstrip("/")
        key = rendered.casefold()
        previous = seen.get(key)
        if previous is not None:
            raise BrainError(
                ErrorCode.PATH_CASE_COLLISION,
                f"Ambiguous enrolled roots in {locator}",
                details={
                    "kind": "root",
                    "first_id": previous[0],
                    "first_root": previous[1],
                    "second_id": entry.id,
                    "second_root": rendered,
                },
                hint="Enroll each repository root once, using one canonical spelling.",
            )
        seen[key] = (entry.id, rendered)
        roots[entry.id] = resolved
    return roots


def discover_catalog(node_map_path: str | Path) -> tuple[NodeMap, tuple[VaultManifest, ...]]:
    """Load one stable node map and all its manifests as one deterministic catalog."""

    node_map = load_node_map(node_map_path)
    locator = str(Path(node_map_path))
    roots = _validate_roots(node_map, locator)
    loaded: list[tuple[str, str, VaultManifest]] = []
    for entry in node_map.vaults:
        root = roots[entry.id]
        snapshot = read_snapshot(root, entry.manifest)
        manifest_locator = f"{root.as_posix()}/{snapshot.relative_path}"
        manifest = vault_manifest_from_mapping(
            parse_yaml(snapshot.text, manifest_locator),
            manifest_locator,
            root=str(root),
        )
        assert_no_case_collisions(list(manifest.index_paths), kind="path")
        for declared_path in manifest.index_paths:
            normalize_relative(declared_path)
        loaded.append((entry.id, entry.root, manifest))

    _duplicate_id(
        tuple((manifest.id, declared_root) for _, declared_root, manifest in loaded),
        locator=locator,
    )
    for enrolled_id, _, manifest in loaded:
        if manifest.id != enrolled_id:
            raise BrainError(
                ErrorCode.CONTRACT_INVALID,
                f"Enrolled ID does not match brain.yaml: {enrolled_id}",
                details={
                    "locator": locator,
                    "enrolled_id": enrolled_id,
                    "manifest_id": manifest.id,
                    "root": manifest.root,
                },
                hint="Make the node map and brain.yaml use the same vault ID.",
            )
    manifests = tuple(sorted((item[2] for item in loaded), key=lambda manifest: manifest.id))
    return node_map, manifests


def discover_vaults(node_map_path: str | Path) -> tuple[VaultManifest, ...]:
    """Discover all enrolled manifests or fail before returning a partial catalog."""

    return discover_catalog(node_map_path)[1]


_LEGACY_VAULT = re.compile(r"^\s*- vault:\s*(.+?)\s*$")
_LEGACY_FIELD = re.compile(r"^\s{4}([a-z][a-z0-9-]*):\s*(.*?)\s*$")
_LEGACY_DOMAIN = re.compile(r"^\s{6}-\s+(.+?)\s*$")


def _legacy_id(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.casefold()).strip("_")
    return f"vault_{normalized}"


def _inline_paths(value: str) -> tuple[str, ...]:
    stripped = value.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return ()
    return tuple(item.strip().strip("'\"") for item in stripped[1:-1].split(",") if item.strip())


def _registry_entries(lines: list[str]) -> list[dict[str, object]]:
    """The registry's ``- vault:`` entries, each as its root plus its four-space fields."""

    raw_entries: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    in_domains = False
    for line in lines:
        vault_match = _LEGACY_VAULT.match(line)
        if vault_match:
            if current is not None:
                raw_entries.append(current)
            current = {"root": vault_match.group(1), "domains": []}
            in_domains = False
            continue
        if current is None:
            continue
        field_match = _LEGACY_FIELD.match(line)
        if field_match:
            key, value = field_match.groups()
            if key == "domains":
                current[key] = list(_inline_paths(value))
                in_domains = True
            else:
                current[key] = value
                in_domains = False
            continue
        if in_domains:
            domain_match = _LEGACY_DOMAIN.match(line)
            if domain_match:
                domains = current["domains"]
                assert isinstance(domains, list)
                domains.append(domain_match.group(1))
                continue
        if line.startswith("  - "):
            raw_entries.append(current)
            current = None
            in_domains = False
    if current is not None:
        raw_entries.append(current)
    return raw_entries


def discover_legacy_registry(
    registry_path: str | Path,
    *,
    node: str = "legacy-local",
) -> tuple[NodeMap, tuple[VaultManifest, ...]]:
    """Adapt the exported MetaVault registry without writing project manifests."""

    locator = str(Path(registry_path))
    raw_entries = _registry_entries(read_local_text(registry_path, max_bytes=2_000_000).splitlines())
    entries: list[NodeEntry] = []
    manifests: list[VaultManifest] = []
    for raw in raw_entries:
        root = validate_root(str(raw["root"]))
        name = str(raw.get("vault-name") or root.name)
        vault_id = _legacy_id(name)
        compass = str(raw.get("compass") or "ops/compass.md")
        exported_paths = _inline_paths(str(raw.get("loads-from") or "")) or (compass,)
        normalized_paths = tuple(normalize_relative(path) for path in exported_paths)
        assert_no_case_collisions(list(normalized_paths), kind="path")
        domains = tuple(str(item) for item in raw.get("domains", []))
        entries.append(NodeEntry(id=vault_id, root=str(root), manifest="brain.yaml"))
        manifests.append(
            VaultManifest(
                schema="brain.vault/v1",
                id=vault_id,
                name=name,
                purpose=f"Legacy vault: {name}",
                topics=domains,
                sensitivity="private",
                sync_class="local-only",
                default_branch="main",
                intent=normalize_relative(compass),
                record_roots=(),
                dependencies=(),
                search=SearchSpec(paths=normalized_paths),
                exports=(ExportSpec(name="legacy-export", paths=normalized_paths),),
                root=str(root),
            )
        )
    _duplicate_id(tuple((entry.id, entry.root) for entry in entries), locator=locator)
    rendered_roots = [entry.root.replace("\\", "/").rstrip("/") for entry in entries]
    assert_no_case_collisions(rendered_roots, kind="root")
    ordered_entries = tuple(sorted(entries, key=lambda entry: entry.id))
    ordered_manifests = tuple(sorted(manifests, key=lambda manifest: manifest.id))
    return NodeMap(schema="brain.node-map/v1", node=node, vaults=ordered_entries), ordered_manifests


# --- MetaVault's registry as the PC connector's enrollment (ROA-72) ------------------------------------
#
# Nic, 28 Sep 2026: every registered vault whose folder exists on the PC is enrolled when the
# connector starts, as ``vault_<name>``, and nothing is written into a vault to enroll it. What agents
# may read in a vault is its declared exports plus its own notes (docs/product/steps/b-notes-in-each-
# vault.md). These functions only read; the connector does the rest.

REGISTRY_SKIP_RETIRED = "retired"
REGISTRY_SKIP_FOLDER_MISSING = "folder_missing"
REGISTRY_SKIP_ROOT_INVALID = "root_invalid"
# Where a vault's readable surfaces came from: its own Vault OS manifest's ``exports:`` (when its
# ``root-path`` is that folder), else the registry entry's ``loads-from``, else its compass.
EXPORTS_FROM_MANIFEST = "manifest"
EXPORTS_FROM_REGISTRY = "registry"
EXPORTS_FROM_COMPASS = "compass"
MAX_REGISTRY_BYTES = 2_000_000
_TOP_KEY = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*):(?:\s+(.*?))?\s*$")
_EXPORT_KEY = re.compile(r"^  ([A-Za-z][A-Za-z0-9_-]*):(?:\s+(.*?))?\s*$")
_EXPORT_ITEM = re.compile(r"^ {2,6}-\s+(.+?)\s*$")


@dataclass(frozen=True, slots=True)
class RegistryVault:
    """One vault the PC connector enrolls: MetaVault itself (from the registry's own header) or a
    ``- vault:`` entry whose folder exists and isn't retired."""

    id: str
    name: str
    root: str
    compass: str
    loads_from: tuple[str, ...] = ()
    topics: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RegistrySkip:
    """A registry entry the connector leaves out, and why: ``retired``, ``folder_missing`` or
    ``root_invalid``."""

    name: str
    root: str
    reason: str


@dataclass(frozen=True, slots=True)
class VaultExports:
    """The paths agents may read in one vault, where they were declared, and any declared path that
    isn't a safe relative path (left out)."""

    paths: tuple[str, ...]
    source: str
    ignored: tuple[str, ...] = ()


def _plain(value: str) -> str:
    """A YAML scalar as written: surrounding quotes, or an inline ``# comment``, dropped."""

    text = value.strip()
    if text[:1] in {"'", '"'}:
        end = text.find(text[0], 1)
        return text[1:end] if end > 0 else text[1:]
    return re.split(r"\s#", text, maxsplit=1)[0].strip()


def _uncommented(value: str) -> str:
    return re.split(r"\s#", value, maxsplit=1)[0].strip()


def _manifest_header(text: str) -> tuple[dict[str, str], list[tuple[str | None, str]] | None]:
    """A Vault OS manifest's top-level scalars, and what its ``exports:`` names, as (name, path)
    pairs (``None`` when there is no ``exports:``). Only these simple shapes are read: ``name:
    path``, ``name: [a, b]``, and ``- path`` items, under a top-level ``exports:``."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, None
    scalars: dict[str, str] = {}
    exports: list[tuple[str | None, str]] | None = None
    in_exports = False
    list_name: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        top = _TOP_KEY.match(line)
        if top:
            key, value = top.group(1), top.group(2) or ""
            in_exports = key == "exports"
            list_name = None
            if in_exports:
                exports = [(None, path) for path in _inline_paths(_uncommented(value))]
            else:
                scalars.setdefault(key, _plain(value))
            continue
        if not in_exports or exports is None:
            continue
        keyed = _EXPORT_KEY.match(line)
        if keyed:
            name, value = keyed.group(1), _uncommented(keyed.group(2) or "")
            list_name = name if not value else None
            if value.startswith("["):
                exports.extend((name, path) for path in _inline_paths(value))
            elif value:
                exports.append((name, _plain(value)))
            continue
        item = _EXPORT_ITEM.match(line)
        if item:
            exports.append((list_name, _plain(item.group(1))))
    return scalars, exports


def _same_folder(declared: str, root: Path) -> bool:
    try:
        return os.path.normcase(str(Path(declared).resolve())) == os.path.normcase(str(root.resolve()))
    except (OSError, ValueError):
        return False


def read_registry(registry_path: str | Path) -> tuple[tuple[RegistryVault, ...], tuple[RegistrySkip, ...]]:
    """Every vault in MetaVault's registry the PC connector enrolls, and the entries it leaves out.

    MetaVault itself comes from the registry's own header (``vault-name``, ``root-path`` and its
    ``exports.compass``): the registry lists only the other vaults. The rest are the ``- vault:``
    entries in any of its lists. An entry marked ``retired:`` is left out (Proposed - Nic to
    confirm), and so is one whose folder isn't on this PC. IDs follow ``discover_legacy_registry``:
    ``vault_`` plus the vault's name. Two vaults with one ID, or one folder listed twice, fail.
    """

    locator = str(Path(registry_path))
    text = read_local_text(registry_path, max_bytes=MAX_REGISTRY_BYTES)
    header, header_exports = _manifest_header(text)
    own_name, own_root = header.get("vault-name"), header.get("root-path")
    if not own_name or not own_root:
        raise BrainError(
            ErrorCode.CONTRACT_INVALID,
            f"Registry header names no vault-name or root-path: {locator}",
            details={"locator": locator},
            hint="The registry is MetaVault's own Vault OS manifest; its header describes MetaVault.",
        )
    own_exports = tuple(path for _, path in header_exports or ())
    own_compass = next((path for name, path in header_exports or () if name == "compass"), "ops/compass.md")
    candidates: list[tuple[str, str, str, tuple[str, ...], tuple[str, ...], bool]] = [
        (own_name, own_root, own_compass, own_exports, (), False),
    ]
    for raw in _registry_entries(text.splitlines()):
        root = str(raw["root"])
        candidates.append((
            str(raw.get("vault-name") or Path(root).name),
            root,
            str(raw.get("compass") or "ops/compass.md"),
            _inline_paths(_uncommented(str(raw.get("loads-from") or ""))),
            tuple(str(item) for item in raw.get("domains", [])),  # type: ignore[union-attr]
            _plain(str(raw.get("retired") or "")).casefold() not in {"", "false", "no", "null", "~"},
        ))
    vaults: list[RegistryVault] = []
    skipped: list[RegistrySkip] = []
    for name, root, compass, loads_from, topics, retired in candidates:
        if retired:
            skipped.append(RegistrySkip(name=name, root=root, reason=REGISTRY_SKIP_RETIRED))
            continue
        if not Path(root).is_dir():
            skipped.append(RegistrySkip(name=name, root=root, reason=REGISTRY_SKIP_FOLDER_MISSING))
            continue
        try:
            resolved = validate_root(root)
        except BrainError as exc:
            skipped.append(RegistrySkip(name=name, root=root, reason=f"{REGISTRY_SKIP_ROOT_INVALID}: {exc.code.value}"))
            continue
        vault_id = _legacy_id(name)
        if vault_id == "vault_":
            raise BrainError(
                ErrorCode.CONTRACT_INVALID,
                f"Registry vault name gives no ID: {name!r}",
                details={"locator": locator, "root": root},
                hint="Give the vault a vault-name with at least one letter or digit.",
            )
        vaults.append(RegistryVault(
            id=vault_id, name=name, root=str(resolved), compass=compass, loads_from=loads_from, topics=topics,
        ))
    _duplicate_id(tuple((vault.id, vault.root) for vault in vaults), locator=locator)
    assert_no_case_collisions([vault.root.replace("\\", "/").rstrip("/") for vault in vaults], kind="root")
    return (
        tuple(sorted(vaults, key=lambda vault: vault.id)),
        tuple(sorted(skipped, key=lambda skip: (skip.name.casefold(), skip.root))),
    )


def vault_exports(vault: RegistryVault) -> VaultExports:
    """What agents may read in one enrolled vault besides its notes (ROA-72 answer 4).

    The vault's own ``ops/vault-manifest.md`` ``exports:`` when that manifest's ``root-path`` is the
    vault's folder (the gate Vault OS uses), else the registry entry's ``loads-from``, else the
    compass. A declared folder stays a folder here; the connector lists the files under it.
    """

    root = Path(vault.root)
    declared: list[str] | None = None
    source = EXPORTS_FROM_COMPASS
    manifest = root / "ops" / "vault-manifest.md"
    if manifest.is_file():
        try:
            header, exports = _manifest_header(read_local_text(manifest, max_bytes=MAX_REGISTRY_BYTES))
        except BrainError:
            header, exports = {}, None
        declared_root = header.get("root-path")
        if exports and declared_root and _same_folder(declared_root, root):
            declared, source = [path for _, path in exports], EXPORTS_FROM_MANIFEST
    if declared is None and vault.loads_from:
        declared, source = list(vault.loads_from), EXPORTS_FROM_REGISTRY
    if declared is None:
        declared, source = [vault.compass], EXPORTS_FROM_COMPASS
    paths: list[str] = []
    ignored: list[str] = []
    for value in declared:
        try:
            normalized = normalize_relative(value.strip().rstrip("/"))
        except BrainError:
            ignored.append(value)
            continue
        if normalized not in paths:
            paths.append(normalized)
    return VaultExports(paths=tuple(paths), source=source, ignored=tuple(ignored))


def registry_manifest(vault: RegistryVault, paths: tuple[str, ...]) -> VaultManifest:
    """The Brain manifest the connector holds for an enrolled vault, exporting exactly ``paths``."""

    try:
        intent: str | None = normalize_relative(vault.compass)
    except BrainError:
        intent = None
    return VaultManifest(
        schema="brain.vault/v1",
        id=vault.id,
        name=vault.name,
        purpose=f"Registered vault: {vault.name}",
        topics=vault.topics,
        sensitivity="private",
        sync_class="local-only",
        default_branch="main",
        intent=intent,
        record_roots=(),
        dependencies=(),
        search=SearchSpec(paths=paths),
        exports=(ExportSpec(name="declared-exports", paths=paths),),
        root=vault.root,
    )
