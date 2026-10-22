"""One-command local practice rollback; never accepts a live Hermes home.

Snapshot and restore the practice home and a version pointer. Keep displaced
files; never delete unknown data. The source/venvs remain separately pinned.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent


def validate(root: Path) -> Path:
    root = root.resolve()
    repo = HERE.parents[4]
    if not root.is_relative_to(repo / ".tmp"):
        raise ValueError("rollback is restricted to repository .tmp")
    try:
        marker = json.loads((root / "practice-only.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ValueError("practice marker required") from None
    if marker != {
        "practice_only": True, "gateways": False, "credentials": False}:
        raise ValueError("practice marker required")
    for p in root.rglob("*"):
        if p.is_symlink():
            raise ValueError("practice symlinks refused")
    return root


def inventory(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file()}


def snapshot(root: Path, name: str) -> Path:
    root = validate(root)
    if not name or not name.replace("-", "").isalnum():
        raise ValueError("invalid snapshot name")
    target = root / "snapshots" / name
    target.mkdir(parents=True, exist_ok=False)
    shutil.copytree(root / "home", target / "home")
    shutil.copyfile(root / "active-version.json", target / "active-version.json")
    (target / "manifest.json").write_text(json.dumps(inventory(target), indent=2) + "\n", encoding="utf-8")
    return target


def restore(root: Path, name: str) -> Path:
    root = validate(root)
    if not name or not name.replace("-", "").isalnum():
        raise ValueError("invalid snapshot name")
    target = root / "snapshots" / name
    pins = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    observed = inventory(target)
    observed.pop("manifest.json")
    if pins != observed:
        raise ValueError("snapshot integrity failed; nothing restored")
    stage = root / ("restore-" + uuid.uuid4().hex)
    shutil.copytree(target / "home", stage)
    retained = root / ("displaced-" + uuid.uuid4().hex)
    (root / "home").rename(retained)
    stage.rename(root / "home")
    shutil.copyfile(target / "active-version.json", root / "active-version.json")
    if inventory(root / "home") != inventory(target / "home"):
        raise ValueError("restored home mismatch; retained preimage available")
    return retained


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=("snapshot", "restore"))
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--name", required=True)
    a = p.parse_args()
    print((snapshot if a.action == "snapshot" else restore)(a.root, a.name))
