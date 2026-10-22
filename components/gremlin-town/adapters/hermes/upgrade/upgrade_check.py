"""Read-only Hermes acceptance inventory. Exit 0=complete, 1=red, 2=needs hub.

Never runs a gateway, model, mail login, save, restart or reset. Runtime probes
run with an empty temporary Hermes home, no inherited credentials and sockets
blocked. Evidence for installed/live behavior must be collected by the hub owner.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def allowed_pin(name: str) -> bool:
    # Explicit allowlist: even an erroneous baseline cannot make this command
    # read auth.json, .env, token files or arbitrary personal data.
    parts = Path(name).parts
    if parts[:2] in (("plugins", "gremlin-town"), ("plugins", "brain")) and len(parts) == 3:
        return parts[2] in {"__init__.py", "plugin.py", "provider.py", "hub_client.py", "plugin.yaml",
                            "settings.json", "plugin-settings.json"}
    if len(parts) == 3 and parts[0] == "skills" and parts[2] == "SKILL.md":
        return parts[1] in {"gremlin-town", "gremlin-town-review", "next-action", "calendar-time"}
    return name in {"skills/.usage.json", "scripts/mail_monitor.py", "scripts/mail_scorer.py",
                   "scripts/mail_monitor_documented.py", "scripts/own-memory.sh", "cron/jobs.json", "brain.json",
                   "memories/MEMORY.md", "memories/USER.md"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def result(check: str, status: str, detail: str) -> dict:
    return {"check": check, "status": status, "detail": detail}


def check(home: Path, python: Path, source: Path, baseline: Path) -> dict:
    rows = []
    expected = json.loads(baseline.read_text(encoding="utf-8"))
    # Baseline is non-secret, created in an isolated practice home or by the hub
    # owner from approved files. Never enumerate an arbitrary real Hermes home.
    pins = expected["pins"]
    for name, pin in pins.items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts or not allowed_pin(name):
            raise ValueError("unsafe baseline path")
        target = home / relative
        if target.is_symlink() or not target.resolve().is_relative_to(home.resolve()):
            raise ValueError("baseline path leaves home")
        ok = target.is_file() and digest(target) == pin
        rows.append(result("pin:" + name, "pass" if ok else "fail", "matches baseline" if ok else "missing or changed"))
    config = home / "config.yaml"
    if config.is_symlink() or not config.resolve().is_relative_to(home.resolve()):
        raise ValueError("config path leaves approved home")
    rows.append(result("config", "pass" if config.is_file() and digest(config) == expected["config_sha256"] else "fail",
                       "exact approved config fingerprint; contents suppressed"))
    # Environment allowlist deliberately drops provider, bot, Gmail, GT, SSH and
    # global Hermes variables. We never read credentials to build this environment.
    environment = {k: v for k, v in os.environ.items() if k.upper() in {
        "SYSTEMROOT", "WINDIR", "PATH", "TEMP", "TMP", "COMSPEC", "PATHEXT"}}
    with tempfile.TemporaryDirectory(prefix="hermes-readcheck-") as scratch:
        environment.update(HERMES_HOME=scratch, HOME=scratch, USERPROFILE=scratch,
                           PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
        done = subprocess.run([str(python), "-B", str(HERE / "runtime_probe.py"),
                               "--home", str(home.resolve()), "--source", str(source.resolve())],
                              env=environment, cwd=scratch, capture_output=True,
                              text=True, encoding="utf-8", timeout=90, check=False)
        try:
            probe = json.loads(done.stdout)
            if done.returncode or not isinstance(probe, list):
                raise ValueError("probe unsuccessful")
            rows.extend(probe)
        except ValueError:
            # Raw stderr can carry plugin data. Report a fixed diagnostic only.
            rows.append(result("runtime", "fail", "probe failed; raw output suppressed"))
    for identifier, detail in json.loads((HERE.parent / "HERMES-RELIANCE.json").read_text(encoding="utf-8"))["hub_checks"].items():
        rows.append(result(identifier, "needs the hub", detail))
    state = "fail" if any(r["status"] == "fail" for r in rows) else "needs the hub" if any(
        r["status"] == "needs the hub" for r in rows) else "pass"
    return {"schema": 1, "status": state, "safe_to_switch": state == "pass", "checks": rows}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--home", type=Path, required=True)
    p.add_argument("--python", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    p.add_argument("--baseline", type=Path, required=True)
    a = p.parse_args()
    try:
        report = check(a.home, a.python, a.source, a.baseline)
    except (OSError, ValueError, KeyError, subprocess.SubprocessError):
        report = {"status": "fail", "safe_to_switch": False,
                  "checks": [result("input", "fail", "invalid input or timed-out probe; details suppressed")]}
    print(json.dumps(report, indent=2))
    return {"pass": 0, "fail": 1, "needs the hub": 2}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
