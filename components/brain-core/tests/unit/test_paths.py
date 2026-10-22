from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

import brain_core.paths as paths
from brain_core.errors import BrainError, ErrorCode
from brain_core.paths import (
    assert_no_case_collisions,
    normalize_relative,
    read_local_text,
    read_snapshot,
    resolve_repository_path,
    validate_node_roots,
    validate_root,
)


def assert_path_error(call, code: ErrorCode) -> BrainError:
    with pytest.raises(BrainError) as caught:
        call()
    assert caught.value.code is code
    return caught.value


def test_normalize_relative_canonicalizes_separators() -> None:
    assert normalize_relative("folder\\nested\\source.md") == "folder/nested/source.md"
    assert normalize_relative("folder//nested/./source.md") == "folder/nested/source.md"


@pytest.mark.parametrize("locator", [None, 1, "", "   ", ".", "./"])
def test_normalize_relative_requires_a_named_file(locator: object) -> None:
    error = assert_path_error(lambda: normalize_relative(locator), ErrorCode.CONTRACT_INVALID)  # type: ignore[arg-type]
    assert error.details["locator"] == locator


@pytest.mark.parametrize(
    "locator",
    [
        "/etc/passwd",
        "C:\\Windows\\system.ini",
        "C:relative-but-drive-qualified.txt",
        "\\\\server\\share\\file.md",
    ],
)
def test_normalize_relative_refuses_posix_windows_drive_and_unc_absolute_paths(locator: str) -> None:
    error = assert_path_error(lambda: normalize_relative(locator), ErrorCode.PATH_ABSOLUTE)
    assert error.details["rule"] == "repository-relative paths only"


@pytest.mark.parametrize("locator", ["..", "../outside.md", "safe/../../outside.md", "safe\\..\\outside.md"])
def test_normalize_relative_refuses_parent_segments(locator: str) -> None:
    error = assert_path_error(lambda: normalize_relative(locator), ErrorCode.PATH_TRAVERSAL)
    assert error.details["locator"] == locator


def test_case_collision_check_accepts_exact_duplicates_and_refuses_case_only_differences() -> None:
    assert_no_case_collisions(["one.md", "one.md", "nested\\two.md", "nested/two.md"], kind="path")
    error = assert_path_error(
        lambda: assert_no_case_collisions(["Folder/Source.md", "folder/source.md"], kind="path"),
        ErrorCode.PATH_CASE_COLLISION,
    )
    assert error.details == {
        "kind": "path",
        "first": "Folder/Source.md",
        "second": "folder/source.md",
    }
    assert error.hint == "Rename one declaration so it differs by more than letter case."


def test_root_collision_check_normalizes_separators_and_trailing_slashes() -> None:
    assert_no_case_collisions(["C:\\Fixture\\", "C:/Fixture"], kind="root")
    error = assert_path_error(
        lambda: assert_no_case_collisions(["C:/Fixture", "c:/fixture/"], kind="root"),
        ErrorCode.PATH_CASE_COLLISION,
    )
    assert error.details["kind"] == "root"


def test_validate_root_requires_a_directory_and_returns_a_resolved_path(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    assert validate_root(root) == root.resolve(strict=True)

    file_path = tmp_path / "file.txt"
    file_path.write_text("not a directory", encoding="utf-8")
    for invalid in (tmp_path / "missing", file_path):
        error = assert_path_error(lambda invalid=invalid: validate_root(invalid), ErrorCode.ROOT_MISSING)
        assert error.details["root"] == str(invalid)


def test_validate_node_roots_preserves_order_and_rejects_case_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    assert validate_node_roots([str(first), str(second)]) == (
        first.resolve(strict=True),
        second.resolve(strict=True),
    )

    rendered = {"one": Path("C:/Synthetic"), "two": Path("c:/synthetic")}
    monkeypatch.setattr(paths, "validate_root", lambda root: rendered[str(root)])
    assert_path_error(lambda: validate_node_roots(["one", "two"]), ErrorCode.PATH_CASE_COLLISION)


def test_resolve_repository_path_returns_file_and_normalized_locator(tmp_path: Path) -> None:
    root = tmp_path / "root"
    source = root / "nested" / "source.md"
    source.parent.mkdir(parents=True)
    source.write_text("synthetic", encoding="utf-8")
    resolved, relative = resolve_repository_path(root, "nested\\source.md")
    assert resolved == source.resolve(strict=True)
    assert relative == "nested/source.md"


@pytest.mark.parametrize("locator", ["missing.md", "ordinary.md/child.md"])
def test_resolve_repository_path_reports_missing_and_not_a_directory(tmp_path: Path, locator: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "ordinary.md").write_text("file", encoding="utf-8")
    error = assert_path_error(lambda: resolve_repository_path(root, locator), ErrorCode.PATH_MISSING)
    assert error.details["locator"] == locator


def test_resolve_repository_path_refuses_a_directory_locator(tmp_path: Path) -> None:
    root = tmp_path / "root"
    directory = root / "nested"
    directory.mkdir(parents=True)
    error = assert_path_error(lambda: resolve_repository_path(root, "nested"), ErrorCode.PATH_MISSING)
    assert error.message == "Declared source is not a regular file: nested"


def test_resolve_repository_path_refuses_symlink_escape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    link = root / "escape.md"
    try:
        link.symlink_to(outside)
    except OSError:
        link.write_text("placeholder", encoding="utf-8")
        real_resolve = Path.resolve

        def fake_resolve(candidate: Path, *, strict: bool = False) -> Path:
            if candidate == link:
                return outside
            return real_resolve(candidate, strict=strict)

        monkeypatch.setattr(Path, "resolve", fake_resolve)

    error = assert_path_error(lambda: resolve_repository_path(root, "escape.md"), ErrorCode.PATH_ESCAPE)
    assert error.details["locator"] == "escape.md"


def test_resolve_repository_path_converts_cross_drive_commonpath_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.md").write_text("source", encoding="utf-8")
    monkeypatch.setattr(paths.os.path, "commonpath", lambda _values: (_ for _ in ()).throw(ValueError()))
    assert_path_error(lambda: resolve_repository_path(root, "source.md"), ErrorCode.PATH_ESCAPE)


def test_read_local_text_accepts_utf8_and_enforces_size(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("Synthetic café", encoding="utf-8")
    assert read_local_text(source, max_bytes=100) == "Synthetic café"
    error = assert_path_error(lambda: read_local_text(source, max_bytes=3), ErrorCode.SOURCE_OVERSIZED)
    assert error.details["bytes"] == len("Synthetic café".encode("utf-8"))
    assert error.details["max_bytes"] == 3


def test_read_local_text_converts_missing_and_invalid_utf8_to_unreadable(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.bin"
    invalid.write_bytes(b"\xff\xfe")
    for source in (tmp_path / "missing.md", invalid):
        error = assert_path_error(lambda source=source: read_local_text(source), ErrorCode.SOURCE_UNREADABLE)
        assert error.details["path"] == str(source)
        assert error.details["error"] in {"FileNotFoundError", "UnicodeDecodeError"}


def test_read_snapshot_returns_a_complete_stable_receipt(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.md"
    content = "Synthetic source.\n"
    source.write_bytes(content.encode("utf-8"))
    expected = hashlib.sha256(content.encode("utf-8")).hexdigest()

    snapshot = read_snapshot(root, "source.md", expected_sha256=expected)
    assert snapshot.path == source.resolve(strict=True)
    assert snapshot.relative_path == "source.md"
    assert snapshot.text == content
    assert snapshot.content_sha256 == expected
    assert snapshot.size == len(content.encode("utf-8"))
    assert snapshot.mtime_ns == source.stat().st_mtime_ns


def test_read_snapshot_refuses_oversized_content_before_read(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.md").write_text("12345", encoding="utf-8")
    error = assert_path_error(
        lambda: read_snapshot(root, "source.md", max_bytes=4), ErrorCode.SOURCE_OVERSIZED
    )
    assert error.details == {"locator": "source.md", "bytes": 5, "max_bytes": 4}


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"text\x00binary", "Binary source excluded: source.md"),
        (b"text\xff", "Non-UTF-8 source excluded: source.md"),
    ],
)
def test_read_snapshot_refuses_null_and_non_utf8_binary_sources(
    tmp_path: Path, content: bytes, message: str
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.md").write_bytes(content)
    error = assert_path_error(lambda: read_snapshot(root, "source.md"), ErrorCode.SOURCE_BINARY)
    assert error.message == message
    if b"\xff" in content:
        assert error.details["offset"] == 4


def test_read_snapshot_converts_io_failure_to_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.md"
    source.write_text("source", encoding="utf-8")
    resolved = source.resolve(strict=True)
    monkeypatch.setattr(paths, "resolve_repository_path", lambda *_args: (resolved, "source.md"))
    real_read_bytes = Path.read_bytes

    def fail_read(candidate: Path) -> bytes:
        if candidate == resolved:
            raise PermissionError("synthetic denial")
        return real_read_bytes(candidate)

    monkeypatch.setattr(Path, "read_bytes", fail_read)
    error = assert_path_error(lambda: read_snapshot(root, "source.md"), ErrorCode.SOURCE_UNREADABLE)
    assert error.details == {"locator": "source.md", "error": "PermissionError"}


def test_read_snapshot_retries_once_when_metadata_changes_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.md"
    source.write_text("stable", encoding="utf-8")
    resolved = source.resolve(strict=True)
    monkeypatch.setattr(paths, "resolve_repository_path", lambda *_args: (resolved, "source.md"))
    states = iter(
        [
            SimpleNamespace(st_size=6, st_mtime_ns=1),
            SimpleNamespace(st_size=6, st_mtime_ns=2),
            SimpleNamespace(st_size=6, st_mtime_ns=2),
            SimpleNamespace(st_size=6, st_mtime_ns=2),
        ]
    )
    real_stat = Path.stat

    def fake_stat(candidate: Path, *args: object, **kwargs: object) -> object:
        if candidate == resolved:
            return next(states)
        return real_stat(candidate, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fake_stat)
    snapshot = read_snapshot(root, "source.md")
    assert snapshot.text == "stable"
    assert snapshot.mtime_ns == 2


def test_read_snapshot_fails_when_metadata_changes_on_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source.md"
    source.write_text("racing", encoding="utf-8")
    resolved = source.resolve(strict=True)
    monkeypatch.setattr(paths, "resolve_repository_path", lambda *_args: (resolved, "source.md"))
    states = iter(
        [
            SimpleNamespace(st_size=6, st_mtime_ns=1),
            SimpleNamespace(st_size=6, st_mtime_ns=2),
            SimpleNamespace(st_size=6, st_mtime_ns=3),
            SimpleNamespace(st_size=6, st_mtime_ns=4),
        ]
    )
    monkeypatch.setattr(Path, "stat", lambda candidate, *_a, **_kw: next(states))
    error = assert_path_error(lambda: read_snapshot(root, "source.md"), ErrorCode.SOURCE_CHANGED)
    assert error.details["expected_sha256"] is None
    assert error.details["actual_sha256"] == hashlib.sha256(b"racing").hexdigest()


def test_read_snapshot_hash_mismatch_retries_then_fails(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.md").write_text("actual", encoding="utf-8")
    expected = hashlib.sha256(b"expected").hexdigest()
    error = assert_path_error(
        lambda: read_snapshot(root, "source.md", expected_sha256=expected), ErrorCode.SOURCE_CHANGED
    )
    assert error.details == {
        "locator": "source.md",
        "expected_sha256": expected,
        "actual_sha256": hashlib.sha256(b"actual").hexdigest(),
    }
    assert error.hint == "Rebuild the index and retry once the source is stable."


def test_read_snapshot_can_disable_retry_for_a_known_hash_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "source.md").write_text("actual", encoding="utf-8")
    assert_path_error(
        lambda: read_snapshot(root, "source.md", expected_sha256="0" * 64, retry_once=False),
        ErrorCode.SOURCE_CHANGED,
    )
