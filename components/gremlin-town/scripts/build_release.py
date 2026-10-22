"""Build a new allowlisted Gremlin Town release tree and tarball."""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import tarfile
from pathlib import Path


ROOT_DIRECTORIES = ("src", "adapters", "deploy", "docs")
ROOT_FILES = ("pyproject.toml", "uv.lock")
FORBIDDEN_PARTS = {
    ".git",
    ".venv",
    ".uv-cache",
    ".pytest_cache",
    ".test-tmp",
    ".artifacts",
    "__pycache__",
    "htmlcov",
}
FORBIDDEN_SUFFIXES = {
    ".db",
    ".sqlite",
    ".sqlite3",
    ".db-wal",
    ".db-shm",
    ".sqlite-wal",
    ".sqlite-shm",
    ".pyc",
    ".pyo",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
}
FORBIDDEN_NAMES = {".env", "credentials", "credentials.json", "secrets.toml"}
SECRET_MARKERS = (b"-----BEGIN PRIVATE KEY-----", b"-----BEGIN OPENSSH PRIVATE KEY-----")


def _files(repository: Path) -> list[Path]:
    selected: list[Path] = []
    for name in ROOT_FILES:
        path = repository / name
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"required packaging file is missing or unsafe: {name}")
        selected.append(path)
    for name in ROOT_DIRECTORIES:
        directory = repository / name
        if not directory.is_dir() or directory.is_symlink():
            raise RuntimeError(f"required packaging directory is missing or unsafe: {name}")
        for path in directory.rglob("*"):
            if path.is_dir():
                continue
            relative = path.relative_to(repository)
            if path.is_symlink():
                raise RuntimeError(f"release input cannot contain symlinks: {relative}")
            if any(part in FORBIDDEN_PARTS for part in relative.parts):
                continue
            if any(character.isspace() for character in relative.as_posix()):
                raise RuntimeError(f"release paths cannot contain whitespace: {relative}")
            lower_name = path.name.lower()
            if lower_name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
                raise RuntimeError(f"release input looks like a secret or runtime artifact: {relative}")
            content = path.read_bytes()
            if any(marker in content for marker in SECRET_MARKERS):
                raise RuntimeError(f"release input contains private-key material: {relative}")
            selected.append(path)
    return sorted(selected, key=lambda path: path.relative_to(repository).as_posix())


def build(repository: Path, output_directory: Path, release_id: str) -> tuple[Path, Path]:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", release_id):
        raise RuntimeError("release id must be 1-80 safe filename characters")
    repository = repository.resolve(strict=True)
    output_directory = output_directory.resolve()
    release_name = f"gremlin-town-{release_id}"
    release_tree = output_directory / release_name
    archive = output_directory / f"{release_name}.tar.gz"
    if release_tree.exists() or archive.exists():
        raise RuntimeError("release destination already exists; choose a new release id")
    output_directory.mkdir(parents=True, exist_ok=True)
    release_tree.mkdir()
    manifest_lines: list[str] = []
    for source in _files(repository):
        relative = source.relative_to(repository)
        destination = release_tree / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        content = destination.read_bytes()
        manifest_lines.append(f"{hashlib.sha256(content).hexdigest()}  {relative.as_posix()}")
    (release_tree / "RELEASE-MANIFEST.sha256").write_text(
        "\n".join(manifest_lines) + "\n", encoding="utf-8", newline="\n"
    )
    with tarfile.open(archive, mode="x:gz") as bundle:
        bundle.add(release_tree, arcname=release_name, recursive=True)
    return release_tree, archive


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release-id", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path(".artifacts"))
    arguments = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    tree, archive = build(repository, arguments.output_dir, arguments.release_id)
    print(f"tree={tree}")
    print(f"archive={archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
