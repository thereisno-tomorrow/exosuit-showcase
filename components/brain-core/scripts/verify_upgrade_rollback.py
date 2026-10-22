"""Build two wheels and prove isolated install, upgrade, and rollback mechanics."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CURRENT = "0.2.0"
PREVIOUS = "0.1.0"


def run(*args: str, env: dict[str, str] | None = None) -> str:
    completed = subprocess.run(args, check=False, text=True, capture_output=True, env=env)
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed ({completed.returncode}): {args!r}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    return completed.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    project = Path(__file__).resolve().parents[1]
    uv = os.environ.get("BRAIN_UV") or shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to verify the application installation contract")
    with tempfile.TemporaryDirectory(prefix="brain-upgrade-") as temporary_text:
        temporary = Path(temporary_text)
        previous_source = temporary / "previous-source"
        shutil.copytree(
            project,
            previous_source,
            ignore=shutil.ignore_patterns(".venv", ".pytest_cache", ".coverage", "dist", "build", "*.egg-info"),
        )
        pyproject = previous_source / "pyproject.toml"
        original = pyproject.read_text(encoding="utf-8")
        pyproject.write_text(original.replace(f'version = "{CURRENT}"', f'version = "{PREVIOUS}"', 1), encoding="utf-8")
        previous_dist = temporary / "previous-dist"
        current_dist = temporary / "current-dist"
        run(
            uv,
            "build",
            "--wheel",
            "--out-dir",
            str(previous_dist),
            str(previous_source),
        )
        run(
            uv,
            "build",
            "--wheel",
            "--out-dir",
            str(current_dist),
            str(project),
        )
        previous_wheel = next(previous_dist.glob("brain_core-*.whl"))
        current_wheel = next(current_dist.glob("brain_core-*.whl"))
        tool_dir = temporary / "tools"
        bin_dir = temporary / "bin"
        env = os.environ.copy()
        env.update(UV_TOOL_DIR=str(tool_dir), UV_TOOL_BIN_DIR=str(bin_dir))
        executable = bin_dir / ("brain.exe" if os.name == "nt" else "brain")
        run(uv, "tool", "install", "--python", sys.executable, "--from", str(previous_wheel), "brain-core", env=env)
        installed = run(str(executable), "--version", env=env)
        run(uv, "tool", "install", "--force", "--python", sys.executable, "--from", str(current_wheel), "brain-core", env=env)
        upgraded = run(str(executable), "--version", env=env)
        run(uv, "tool", "install", "--force", "--python", sys.executable, "--from", str(previous_wheel), "brain-core", env=env)
        rolled_back = run(str(executable), "--version", env=env)
        expected = (f"brain {PREVIOUS}", f"brain {CURRENT}", f"brain {PREVIOUS}")
        actual = (installed, upgraded, rolled_back)
        if actual != expected:
            raise RuntimeError(f"version transition mismatch: expected {expected}, got {actual}")
        print(
            json.dumps(
                {
                    "result": "ok",
                    "installer": "uv tool",
                    "installed": installed,
                    "upgraded": upgraded,
                    "rolled_back": rolled_back,
                    "previous_wheel_sha256": sha256(previous_wheel),
                    "current_wheel_sha256": sha256(current_wheel),
                },
                sort_keys=True,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
