"""Show the actual installed version selected by a practice rollback pointer.

No arbitrary command forwarding, gateways or model runs are supported.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

from rollback import validate


def version(root: Path) -> str:
    root = validate(root)
    pointer = json.loads((root / "active-version.json").read_text(encoding="utf-8"))
    source = Path(pointer["source"]).resolve()
    repo = Path(__file__).resolve().parent.parents[4]
    if not source.is_relative_to(repo / ".tmp"):
        raise ValueError("source outside local practice boundary")
    python = source / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    environment = {k: v for k, v in os.environ.items() if k.upper() in {
        "SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "PATHEXT"}}
    environment.update(HERMES_HOME=str(root / "home"), HOME=str(root), USERPROFILE=str(root), PYTHONDONTWRITEBYTECODE="1")
    observed = subprocess.run([str(python), "-B", "-c", "import importlib.metadata; print(importlib.metadata.version('hermes-agent'))"],
                              capture_output=True, text=True, encoding="utf-8", check=True,
                              env=environment, cwd=root, timeout=30).stdout.strip()
    # The sandbox user differs from the download owner. Trust only this checked
    # practice source for this one read; no persistent Git trust enrollment.
    commit = subprocess.run(["git", "-c", "safe.directory=" + source.as_posix(), "-C", str(source), "rev-parse", "HEAD"], capture_output=True,
                            text=True, check=True, env=environment, timeout=30).stdout.strip()
    if observed != pointer["version"] or commit != pointer["commit"]:
        raise ValueError("pinned source or installed version differs")
    return observed


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    a = p.parse_args()
    print(version(a.root))
