"""Build a credential-free practice home from repository-owned files only.

Install the pinned public Hermes source/venv separately with uv sync --frozen.
This script never reads the live home or Brain Home and never starts a gateway.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADAPTER = HERE.parent


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def prepare(root: Path) -> Path:
    repo = HERE.parents[4]
    root = root.resolve()
    if not root.is_relative_to(repo / ".tmp") or root.exists():
        raise ValueError("practice target must be a new directory inside this repository's .tmp")
    home = root / "home"
    home.mkdir(parents=True)
    for package, origin in (("gremlin-town", ADAPTER), ("brain", ADAPTER / "memory_brain")):
        manifest = json.loads((origin / "install-manifest.json").read_text(encoding="utf-8"))
        for entry in manifest["files"]:
            src = HERE.parents[2] / entry["source"]
            release_bytes = src.read_bytes().replace(b"\r\n", b"\n")
            if hashlib.sha256(release_bytes).hexdigest() != entry["sha256"]:
                raise ValueError("repository plugin source differs from release manifest: " + entry["source"])
            dest = home / "plugins" / package / Path(entry["source"]).name
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Release archives are LF. Copy logical bytes, never external files.
            dest.write_bytes(release_bytes)
        write(home / "plugins" / package / "settings.json", {"hub_url": "https://practice.invalid", "token_file": str(home / "absent-fixture-token")})
    write(home / "plugins/gremlin-town/plugin-settings.json", {"enabled_optional_features": ["orientation", "review", "calendar"], "tool_visibility": "brain-and-more"})
    usage = {}
    skill_pins = {Path(e["source"]).parts[-2]: e["sha256"] for e in json.loads(
        (ADAPTER / "install-manifest.json").read_text(encoding="utf-8"))["skill_files"]}
    for skill in sorted((ADAPTER / "skills").iterdir()):
        if not skill.is_dir():
            continue
        dest = home / "skills" / skill.name / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        release_bytes = (skill / "SKILL.md").read_bytes().replace(b"\r\n", b"\n")
        if hashlib.sha256(release_bytes).hexdigest() != skill_pins[skill.name]:
            raise ValueError("repository skill differs from release manifest: " + skill.name)
        dest.write_bytes(release_bytes)
        usage[skill.name] = {"pinned": True, "state": "active", "created_by": None}
    write(home / "skills/.usage.json", usage)
    for script in (ADAPTER / "scripts").iterdir():
        if script.is_file():
            dest = home / "scripts" / script.name
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(script.read_bytes().replace(b"\r\n", b"\n"))
    shutil.copyfile(HERE / "config-candidate.yaml", home / "config.yaml")
    write(home / "brain.json", {"prefetch": "on"})
    for name in ("MEMORY.md", "USER.md"):
        dest = home / "memories" / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("Practice fixture only. No personal data.\n", encoding="utf-8")
    write(home / "cron/jobs.json", {"jobs": [
        {"id": "fixture-own-memory", "name": "own-memory-check", "enabled": False,
         "schedule": {"type": "cron", "expr": "30 21 * * *"}, "monitor_script": "scripts/own-memory.sh"},
        {"id": "fixture-mail", "name": "important-mail", "enabled": False,
         "schedule": {"type": "interval", "minutes": 3}, "no_agent": True,
         "script": "scripts/mail_monitor_documented.py"}]})
    write(root / "practice-only.json", {"practice_only": True, "gateways": False, "credentials": False})
    pins = {str(p.relative_to(home)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in home.rglob("*") if p.is_file() and p.name != "config.yaml"}
    write(root / "baseline.json", {"schema": 1, "pins": pins,
         "config_sha256": hashlib.sha256((home / "config.yaml").read_bytes()).hexdigest()})
    return home


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    args = p.parse_args()
    print(prepare(args.root))
