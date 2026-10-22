"""The only filesystem read boundary for repositories and local config."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath

from brain_core.errors import BrainError, ErrorCode


@dataclass(frozen=True, slots=True)
class FileSnapshot:
    path: Path
    relative_path: str
    text: str
    content_sha256: str
    size: int
    mtime_ns: int


def normalize_relative(locator: str) -> str:
    if not isinstance(locator, str) or not locator.strip():
        raise BrainError(
            ErrorCode.CONTRACT_INVALID,
            "Repository locator must be a non-empty string",
            details={"locator": locator},
        )
    windows = PureWindowsPath(locator)
    posix = PurePosixPath(locator.replace("\\", "/"))
    if windows.is_absolute() or windows.drive or posix.is_absolute():
        raise BrainError(
            ErrorCode.PATH_ABSOLUTE,
            f"Absolute repository path refused: {locator}",
            details={"locator": locator, "rule": "repository-relative paths only"},
        )
    if ".." in posix.parts:
        raise BrainError(
            ErrorCode.PATH_TRAVERSAL,
            f"Traversal refused: {locator}",
            details={"locator": locator, "rule": "no parent segments"},
        )
    normalized = posix.as_posix()
    if normalized in {".", ""}:
        raise BrainError(
            ErrorCode.CONTRACT_INVALID,
            "Repository locator must name a file",
            details={"locator": locator},
        )
    return normalized


def assert_no_case_collisions(values: tuple[str, ...] | list[str], *, kind: str) -> None:
    seen: dict[str, str] = {}
    for raw in values:
        normalized = normalize_relative(raw) if kind == "path" else raw.replace("\\", "/").rstrip("/")
        key = normalized.casefold()
        previous = seen.get(key)
        if previous is not None and previous != normalized:
            raise BrainError(
                ErrorCode.PATH_CASE_COLLISION,
                f"Case-colliding {kind}s refused",
                details={"kind": kind, "first": previous, "second": normalized},
                hint="Rename one declaration so it differs by more than letter case.",
            )
        seen[key] = normalized


def validate_root(root: str | Path) -> Path:
    candidate = Path(root)
    if not candidate.is_dir():
        raise BrainError(
            ErrorCode.ROOT_MISSING,
            f"Repository root is missing: {candidate}",
            details={"root": str(candidate)},
        )
    return candidate.resolve(strict=True)


def validate_node_roots(roots: tuple[str, ...] | list[str]) -> tuple[Path, ...]:
    resolved = tuple(validate_root(root) for root in roots)
    rendered = [str(path).replace("\\", "/").rstrip("/") for path in resolved]
    assert_no_case_collisions(rendered, kind="root")
    return resolved


def resolve_repository_path(root: str | Path, locator: str) -> tuple[Path, str]:
    repository = validate_root(root)
    relative = normalize_relative(locator)
    candidate = repository.joinpath(*PurePosixPath(relative).parts)
    try:
        resolved = candidate.resolve(strict=True)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise BrainError(
            ErrorCode.PATH_MISSING,
            f"Declared source is missing: {relative}",
            details={"root": str(repository), "locator": relative},
        ) from exc
    try:
        common = os.path.commonpath((os.path.normcase(str(repository)), os.path.normcase(str(resolved))))
    except ValueError as exc:
        raise BrainError(
            ErrorCode.PATH_ESCAPE,
            f"Repository boundary escape refused: {relative}",
            details={"root": str(repository), "locator": relative},
        ) from exc
    if common != os.path.normcase(str(repository)):
        raise BrainError(
            ErrorCode.PATH_ESCAPE,
            f"Repository boundary escape refused: {relative}",
            details={"root": str(repository), "locator": relative},
        )
    if not resolved.is_file():
        raise BrainError(
            ErrorCode.PATH_MISSING,
            f"Declared source is not a regular file: {relative}",
            details={"root": str(repository), "locator": relative},
        )
    return resolved, relative


def read_local_text(path: str | Path, *, max_bytes: int = 1_000_000) -> str:
    candidate = Path(path)
    try:
        size = candidate.stat().st_size
        if size > max_bytes:
            raise BrainError(
                ErrorCode.SOURCE_OVERSIZED,
                f"Local file exceeds {max_bytes} bytes: {candidate}",
                details={"path": str(candidate), "bytes": size, "max_bytes": max_bytes},
            )
        return candidate.read_text(encoding="utf-8")
    except BrainError:
        raise
    except (OSError, UnicodeError) as exc:
        raise BrainError(
            ErrorCode.SOURCE_UNREADABLE,
            f"Cannot read local file: {candidate}",
            details={"path": str(candidate), "error": type(exc).__name__},
        ) from exc


def read_snapshot(
    root: str | Path,
    locator: str,
    *,
    max_bytes: int = 1_000_000,
    expected_sha256: str | None = None,
    retry_once: bool = True,
) -> FileSnapshot:
    resolved, relative = resolve_repository_path(root, locator)
    try:
        before = resolved.stat()
        if before.st_size > max_bytes:
            raise BrainError(
                ErrorCode.SOURCE_OVERSIZED,
                f"Source exceeds {max_bytes} bytes: {relative}",
                details={"locator": relative, "bytes": before.st_size, "max_bytes": max_bytes},
            )
        content = resolved.read_bytes()
        after = resolved.stat()
    except BrainError:
        raise
    except OSError as exc:
        raise BrainError(
            ErrorCode.SOURCE_UNREADABLE,
            f"Cannot read source: {relative}",
            details={"locator": relative, "error": type(exc).__name__},
        ) from exc
    if b"\x00" in content[:8192]:
        raise BrainError(
            ErrorCode.SOURCE_BINARY,
            f"Binary source excluded: {relative}",
            details={"locator": relative},
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise BrainError(
            ErrorCode.SOURCE_BINARY,
            f"Non-UTF-8 source excluded: {relative}",
            details={"locator": relative, "offset": exc.start},
        ) from exc
    digest = hashlib.sha256(content).hexdigest()
    changed_during_read = (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
    hash_mismatch = expected_sha256 is not None and digest != expected_sha256
    if changed_during_read or hash_mismatch:
        if retry_once:
            return read_snapshot(
                root,
                locator,
                max_bytes=max_bytes,
                expected_sha256=expected_sha256,
                retry_once=False,
            )
        raise BrainError(
            ErrorCode.SOURCE_CHANGED,
            f"Source changed while assembling context: {relative}",
            details={
                "locator": relative,
                "expected_sha256": expected_sha256,
                "actual_sha256": digest,
            },
            hint="Rebuild the index and retry once the source is stable.",
        )
    return FileSnapshot(
        path=resolved,
        relative_path=relative,
        text=text,
        content_sha256=digest,
        size=len(content),
        mtime_ns=after.st_mtime_ns,
    )
