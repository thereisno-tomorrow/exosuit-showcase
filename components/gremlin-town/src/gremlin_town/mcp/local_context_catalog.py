"""On-request folder → A02 retrieval catalog generation for the PC connector."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

CATALOG_SCHEMA = "brain.a02-retrieval-catalog/v1"
_EVIDENCE_ID_SAFE = re.compile(r"[^A-Za-z0-9._:-]+")
_DEFAULT_EXTENSIONS = frozenset({".md"})
_SKIP_DIR_NAMES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".brain",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".tox",
        "dist",
        "build",
        ".idea",
        ".vscode",
    }
)
_SKIP_NAME_PARTS = ("secret", "credential", "token", "private-key", ".env")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True, slots=True)
class FolderCatalogLimits:
    max_eligible_files: int = 200
    max_enumerated_entries: int = 2000
    max_file_bytes: int = 262_144
    max_total_content_bytes: int = 8_388_608

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "FolderCatalogLimits":
        if not value:
            return cls()
        return cls(
            max_eligible_files=int(value.get("max_eligible_files", 200)),
            max_enumerated_entries=int(value.get("max_enumerated_entries", 2000)),
            max_file_bytes=int(value.get("max_file_bytes", 262_144)),
            max_total_content_bytes=int(value.get("max_total_content_bytes", 8_388_608)),
        )


@dataclass
class ScanReport:
    enumerated_entries: int = 0
    eligible_files: int = 0
    included_files: int = 0
    total_content_bytes: int = 0
    limited: bool = False
    exclusions: list[dict[str, Any]] = field(default_factory=list)

    def exclude(self, code: str, path: str, reason: str) -> None:
        self.exclusions.append({"code": code, "path": path, "reason": reason})


@dataclass(frozen=True, slots=True)
class ScanResult:
    catalog: dict[str, Any]
    report: ScanReport
    path_hashes: tuple[tuple[str, str], ...]


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _LOCKS[key] = lock
        return lock


def evidence_id_for(relative_path: str) -> str:
    cleaned = _EVIDENCE_ID_SAFE.sub("-", relative_path.replace("\\", "/"))
    candidate = f"md:{cleaned}"
    if len(candidate) > 128:
        digest = hashlib.sha256(relative_path.encode("utf-8")).hexdigest()[:16]
        candidate = f"md:{digest}-{cleaned}"[:128]
    if not candidate[0].isalnum():
        candidate = f"E{candidate[1:]}"
    return candidate


def _is_hidden_part(part: str) -> bool:
    return part.startswith(".") and part not in {".", ".."}


def _secretish(name: str) -> bool:
    lowered = name.casefold()
    return any(token in lowered for token in _SKIP_NAME_PARTS)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _reject_reparse_components(path: Path) -> None:
    # Inspect the lexical path before resolve() can erase a symlink/junction.
    for component in (*reversed(path.parents), path):
        info = component.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise ValueError("symlink or reparse-point scan path refused")


def _checked_scan_path(root: Path, path: Path) -> None:
    # Both the lexical name and actual destination must stay in this folder,
    # not merely somewhere inside the broader vault.
    path.relative_to(root)
    _reject_reparse_components(path)
    path.resolve(strict=True).relative_to(root)


def resolve_scan_root(vault_root: Path, scan_root: str) -> Path:
    raw = PurePosixPath(scan_root if scan_root else ".")
    if raw.is_absolute() or ".." in raw.parts or "\\" in scan_root or ":" in scan_root:
        raise ValueError("scan_root must be a relative POSIX path without '..'")
    vault = vault_root.absolute()
    target = vault / Path(*raw.parts)
    _reject_reparse_components(target)
    resolved_vault = vault.resolve(strict=True)
    resolved_target = target.resolve(strict=True)
    resolved_target.relative_to(resolved_vault)
    if not resolved_target.is_dir():
        raise ValueError("scan_root is not a directory")
    return target


def scan_markdown_folder(
    *,
    vault_root: Path,
    scan_root: str,
    owner_vault: str,
    limits: FolderCatalogLimits | None = None,
    extensions: Iterable[str] | None = None,
) -> ScanResult:
    limits = limits or FolderCatalogLimits()
    ext_set = frozenset(
        e.lower() if e.startswith(".") else f".{e.lower()}"
        for e in (extensions or _DEFAULT_EXTENSIONS)
    )
    root = resolve_scan_root(vault_root, scan_root)
    vault = vault_root.absolute()
    report = ScanReport()
    included: list[tuple[str, str, int]] = []

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)
        _checked_scan_path(root, current)
        kept: list[str] = []
        for name in list(dirnames):
            report.enumerated_entries += 1
            if report.enumerated_entries > limits.max_enumerated_entries:
                report.limited = True
                report.exclude("ENUM_LIMIT", str(current / name), "enumeration cap reached")
                dirnames.clear()
                kept = []
                break
            if name in _SKIP_DIR_NAMES or _is_hidden_part(name) or _secretish(name):
                report.exclude("DIR_EXCLUDED", str(current / name), "skipped directory")
                continue
            child = current / name
            try:
                _checked_scan_path(root, child)
            except (ValueError, OSError):
                report.exclude("ESCAPE_EXCLUDED", str(child), "reparse or out-of-folder directory")
                continue
            kept.append(name)
        dirnames[:] = kept
        if report.limited and not dirnames:
            # still process files in current dir unless enum already exhausted mid-dir
            pass
        if report.limited and report.enumerated_entries > limits.max_enumerated_entries:
            break

        for name in filenames:
            report.enumerated_entries += 1
            if report.enumerated_entries > limits.max_enumerated_entries:
                report.limited = True
                report.exclude("ENUM_LIMIT", str(current / name), "enumeration cap reached")
                break
            path = current / name
            rel = path.relative_to(vault).as_posix()
            if _is_hidden_part(name) or _secretish(name):
                report.exclude("FILE_EXCLUDED", rel, "hidden or secret-ish name")
                continue
            if path.suffix.lower() not in ext_set:
                report.exclude("EXT_EXCLUDED", rel, "extension not allowed")
                continue
            try:
                _checked_scan_path(root, path)
            except (ValueError, OSError):
                report.exclude("ESCAPE_EXCLUDED", rel, "reparse or out-of-folder file")
                continue
            report.eligible_files += 1
            if report.eligible_files > limits.max_eligible_files:
                report.limited = True
                report.exclude("FILE_LIMIT", rel, "eligible file cap reached")
                continue
            try:
                size = path.stat().st_size
            except OSError as exc:
                report.exclude("STAT_FAILED", rel, str(exc))
                continue
            if size > limits.max_file_bytes:
                report.limited = True
                report.exclude("FILE_TOO_LARGE", rel, "per-file byte cap")
                continue
            if report.total_content_bytes + size > limits.max_total_content_bytes:
                report.limited = True
                report.exclude("TOTAL_BYTES_LIMIT", rel, "total content byte cap")
                continue
            _checked_scan_path(root, path)
            try:
                data = path.read_bytes()
            except OSError as exc:
                report.exclude("READ_FAILED", rel, str(exc))
                continue
            if b"\x00" in data:
                report.exclude("BINARY_EXCLUDED", rel, "nul byte in file")
                continue
            digest = _sha256_bytes(data)
            included.append((rel, digest, size))
            report.total_content_bytes += size
            report.included_files += 1

    included.sort(key=lambda item: item[0])
    version_material = json.dumps(
        [[rel, digest] for rel, digest, _ in included],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    repository_version = "folder-scan-" + _sha256_bytes(version_material)
    for relative, _, _ in included:
        _checked_scan_path(root, vault / relative)
    evidence = [
        {
            "evidence_id": evidence_id_for(rel),
            "owner_vault": owner_vault,
            "relative_path": rel,
            "content_sha256": digest,
            "state": "current",
        }
        for rel, digest, _ in included
    ]
    catalog = {
        "schema": CATALOG_SCHEMA,
        "repository_version": repository_version,
        "evidence": evidence,
        "links": [],
    }
    return ScanResult(catalog, report, tuple((rel, digest) for rel, digest, _ in included))


def publish_catalog_atomic(catalog_path: Path, catalog: Mapping[str, Any]) -> None:
    catalog_path = Path(catalog_path)
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(dict(catalog), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    lock = _lock_for(catalog_path)
    with lock:
        tmp = catalog_path.with_name(catalog_path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, catalog_path)


def refresh_folder_catalog(
    *,
    vault_root: Path,
    catalog_path: Path,
    owner_vault: str,
    scan_root: str = ".",
    limits: FolderCatalogLimits | None = None,
    extensions: Iterable[str] | None = None,
) -> ScanResult:
    result = scan_markdown_folder(
        vault_root=vault_root,
        scan_root=scan_root,
        owner_vault=owner_vault,
        limits=limits,
        extensions=extensions,
    )
    again = scan_markdown_folder(
        vault_root=vault_root,
        scan_root=scan_root,
        owner_vault=owner_vault,
        limits=limits,
        extensions=extensions,
    )
    selected = again if again.path_hashes != result.path_hashes else result
    publish_catalog_atomic(catalog_path, selected.catalog)
    return selected


# --- A registry vault's declared exports (ROA-72) ---------------------------------------------------------


def scan_exported_paths(
    *,
    vault_root: Path,
    exports: Iterable[str],
    owner_vault: str,
    limits: FolderCatalogLimits | None = None,
) -> ScanResult:
    """The Markdown files a registry vault's declared exports name (ROA-72, "Exports + notes").

    A file export is that file; a folder export is the Markdown under it, with the folder scan's
    skips. An export that is missing, hidden, secret-looking, a link or outside the vault is noted
    in the report and left out, never an error. The file-count and total-size limits hold across
    all of the vault's exports together, in path order.
    """

    limits = limits or FolderCatalogLimits()
    vault = vault_root.absolute()
    resolved_vault = vault.resolve(strict=True)
    report = ScanReport()
    found: dict[str, str] = {}
    for export in exports:
        raw = PurePosixPath(export)
        if not raw.parts or raw.is_absolute() or ".." in raw.parts or "\\" in export or ":" in export:
            report.exclude("EXPORT_UNSAFE", export, "an export must be a relative path inside the vault")
            continue
        if any(_is_hidden_part(part) or _secretish(part) for part in raw.parts):
            report.exclude("EXPORT_EXCLUDED", export, "hidden or secret-ish export")
            continue
        target = vault / Path(*raw.parts)
        if not target.exists():
            report.exclude("EXPORT_MISSING", export, "declared export not found")
            continue
        try:
            _reject_reparse_components(target)
            target.resolve(strict=True).relative_to(resolved_vault)
        except (ValueError, OSError):
            report.exclude("ESCAPE_EXCLUDED", export, "reparse or out-of-vault export")
            continue
        if target.is_dir():
            scanned = scan_markdown_folder(
                vault_root=vault_root, scan_root=raw.as_posix(), owner_vault=owner_vault, limits=limits
            )
            report.enumerated_entries += scanned.report.enumerated_entries
            report.limited = report.limited or scanned.report.limited
            report.exclusions.extend(scanned.report.exclusions)
            for relative, digest in scanned.path_hashes:
                found.setdefault(relative, digest)
            continue
        relative = raw.as_posix()
        report.enumerated_entries += 1
        if target.suffix.lower() not in _DEFAULT_EXTENSIONS:
            report.exclude("EXT_EXCLUDED", relative, "extension not allowed")
            continue
        try:
            size = target.stat().st_size
        except OSError as exc:
            report.exclude("STAT_FAILED", relative, str(exc))
            continue
        if size > limits.max_file_bytes:
            report.limited = True
            report.exclude("FILE_TOO_LARGE", relative, "per-file byte cap")
            continue
        try:
            data = target.read_bytes()
        except OSError as exc:
            report.exclude("READ_FAILED", relative, str(exc))
            continue
        if b"\x00" in data:
            report.exclude("BINARY_EXCLUDED", relative, "nul byte in file")
            continue
        found.setdefault(relative, _sha256_bytes(data))

    included: list[tuple[str, str]] = []
    total = 0
    for relative in sorted(found):
        try:
            size = (vault / relative).stat().st_size
        except OSError as exc:
            report.exclude("STAT_FAILED", relative, str(exc))
            continue
        if len(included) >= limits.max_eligible_files:
            report.limited = True
            report.exclude("FILE_LIMIT", relative, "eligible file cap reached")
            continue
        if total + size > limits.max_total_content_bytes:
            report.limited = True
            report.exclude("TOTAL_BYTES_LIMIT", relative, "total content byte cap")
            continue
        included.append((relative, found[relative]))
        total += size
    report.eligible_files = len(found)
    report.included_files = len(included)
    report.total_content_bytes = total
    version_material = json.dumps(
        [[relative, digest] for relative, digest in included], ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    catalog = {
        "schema": CATALOG_SCHEMA,
        "repository_version": "exports-scan-" + _sha256_bytes(version_material),
        "evidence": [
            {
                "evidence_id": evidence_id_for(relative),
                "owner_vault": owner_vault,
                "relative_path": relative,
                "content_sha256": digest,
                "state": "current",
            }
            for relative, digest in included
        ],
        "links": [],
    }
    return ScanResult(catalog, report, tuple(included))


def refresh_exports_catalog(
    *,
    vault_root: Path,
    catalog_path: Path,
    owner_vault: str,
    exports: Iterable[str],
    limits: FolderCatalogLimits | None = None,
) -> ScanResult:
    """Scan a registry vault's exports twice, keep the later scan if a file changed in between, and
    publish the catalog at ``catalog_path`` (in the connector's state folder, never the vault)."""

    declared = tuple(exports)
    result = scan_exported_paths(vault_root=vault_root, exports=declared, owner_vault=owner_vault, limits=limits)
    again = scan_exported_paths(vault_root=vault_root, exports=declared, owner_vault=owner_vault, limits=limits)
    selected = again if again.path_hashes != result.path_hashes else result
    publish_catalog_atomic(catalog_path, selected.catalog)
    return selected
