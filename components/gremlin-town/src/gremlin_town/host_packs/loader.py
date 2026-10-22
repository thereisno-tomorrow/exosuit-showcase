"""Bounded, read-only candidate loading. Never proves native installation."""
from __future__ import annotations
import hashlib
import json
import stat
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

MAX_BYTES = 65536

def _read(root: Path, relative: str) -> bytes:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise ValueError("invalid relative path")
    rel = PurePosixPath(relative)
    if rel.is_absolute() or PureWindowsPath(relative).drive or ".." in rel.parts or ":" in relative:
        raise ValueError("path escape refused")
    path = root
    for part in rel.parts:
        path = path / part
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("reparse path refused")
    path.resolve(strict=True).relative_to(root)
    if not path.is_file():
        raise ValueError("regular file required")
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("file size limit")
    data.decode("utf-8")
    return data

def dry_load_host_packs(root: Path) -> dict:
    root = Path(root)
    if root.is_symlink() or root.is_junction():
        raise ValueError("reparse root refused")
    root = root.resolve(strict=True)
    data = json.loads(_read(root, "pack.manifest.json"))
    if data.get("schema") != "gt.host-preparation/v1" or data.get("status") != "candidate":
        raise ValueError("candidate manifest required")
    paths = data.get("markdown_paths")
    if not isinstance(paths, list) or not paths or len(paths) > 16:
        raise ValueError("bounded paths required")
    if any(not isinstance(p, str) or not p.endswith(".md") for p in paths):
        raise ValueError("Markdown paths required")
    if len(set(paths)) != len(paths):
        raise ValueError("duplicate paths")
    hashes = {p: hashlib.sha256(_read(root, p)).hexdigest() for p in paths}
    return {"schema": "gt.host-preparation-dry-load/v1", "status": "dry-loaded",
            "native_loaded": False, "installed": False, "auto_activate": False,
            "sha256": hashes}

if __name__ == "__main__":
    print(json.dumps(dry_load_host_packs(Path(sys.argv[1])), indent=2))
