#!/usr/bin/env python3
"""One checked hub release step per invocation; never retries or rolls back.

Run as root with the host's Python 3.13. Input plan and all outputs stay on
the hub. stdout contains only fixed status text, never subprocess output.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tarfile
import time
import urllib.request


class Stop(RuntimeError):
    pass


def require(ok: bool, message: str) -> None:
    if not ok:
        raise Stop(message)


def digest(path: Path) -> str:
    require(path.is_file() and not path.is_symlink(), "expected regular file missing")
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def copy_stable_file(source: Path, target: Path) -> str:
    before = digest(source)
    shutil.copyfile(source, target)
    require(before == digest(source) == digest(target), "mutable file changed during backup")
    return before


def backup_tree(source: Path, target: Path) -> None:
    """Snapshot real databases; retain orphan sidecars as checked ordinary files."""
    require(source.is_dir() and not source.is_symlink(), "mutable state directory missing or redirected")
    target.mkdir(mode=0o700)
    orphans = []
    entries = sorted(source.iterdir())
    initial_paths = frozenset(entries)
    for path in entries:
        require(not path.is_symlink(), "mutable state contains a link; inspect before backup")
        destination = target / path.name
        if path.is_dir():
            backup_tree(path, destination)
            continue
        require(path.is_file(), "mutable state contains a special file")
        if path.name.endswith(("-wal", "-shm")):
            base = path.with_name(path.name.rsplit("-", 1)[0])
            require(not base.is_symlink(), "SQLite sidecar base is a link")
            if base not in initial_paths:
                require(not base.exists(), "orphan SQLite sidecar base appeared during backup")
                # No database owns these bytes. Preserve them without attaching,
                # checkpointing, deleting or claiming a healthy database backup.
                signature = copy_stable_file(path, destination)
                require(not base.exists() and not base.is_symlink(), "orphan SQLite sidecar base appeared during backup")
                orphans.append((path, destination, base, signature))
                continue
            require(base.exists(), "SQLite sidecar base disappeared during backup")
            require(base.is_file(), "SQLite sidecar base is not a regular file")
            with base.open("rb") as handle:
                require(handle.read(16) == b"SQLite format 3\x00", "unrecognized SQLite sidecar")
            continue
        with path.open("rb") as handle:
            sqlite = handle.read(16) == b"SQLite format 3\x00"
        if sqlite:
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as incoming:
                with closing(sqlite3.connect(destination)) as outgoing:
                    incoming.backup(outgoing)
                    require(outgoing.execute("PRAGMA integrity_check").fetchone() == ("ok",), "database backup failed integrity check")
                    outgoing.execute("PRAGMA journal_mode=DELETE")
        else:
            copy_stable_file(path, destination)
    # A later sibling copy must not hide a newly created base or a changed orphan.
    for path, destination, base, signature in orphans:
        require(not base.exists() and not base.is_symlink(), "orphan SQLite sidecar base appeared during backup")
        require(signature == digest(path) == digest(destination), "orphan SQLite sidecar changed during backup")


def run(args: list[str], *, output=None) -> bytes:
    """Never echo command arguments or tool errors, which may contain private data."""
    result = subprocess.run(args, stdout=output or subprocess.PIPE, stderr=subprocess.PIPE)
    require(result.returncode == 0, "command failed; inspect privately before any further change")
    return result.stdout or b""


def ready() -> bool:
    try:
        # No redirect: a response from any other route is a surprise.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        with urllib.request.build_opener(NoRedirect, urllib.request.ProxyHandler({})).open(
            "http://127.0.0.1:8765/health/ready", timeout=1
        ) as response:
            return response.status == 200
    except (OSError, ValueError):
        return False


class Release:
    def __init__(self, plan: dict, root: Path = Path("/")):
        require(set(plan) == {"new", "previous", "commit", "files", "records_sha256"}, "unexpected release plan fields")
        for key in ("new", "previous"):
            require(isinstance(plan[key], str) and bool(re.fullmatch(r"[0-9]{8}-[a-z0-9-]+", plan[key])), "invalid release name")
        require(plan["new"] != plan["previous"], "release names must differ")
        require(bool(re.fullmatch(r"[a-f0-9]{40}", plan["commit"])), "invalid source commit")
        require(bool(re.fullmatch(r"[a-f0-9]{64}", plan["records_sha256"])), "invalid frozen record hash")
        self.names = {"brain_core-0.4.0-py3-none-any.whl", "gremlin_town-0.1.0-py3-none-any.whl", "runtime-requirements.txt", "deploy.tar"}
        require(set(plan["files"]) == self.names and all(isinstance(v, str) and re.fullmatch(r"[a-f0-9]{64}", v) for v in plan["files"].values()), "invalid artifact inventory")
        self.plan, self.root = plan, root
        self.releases = root / "opt/gremlin-town/releases"
        self.current = root / "opt/gremlin-town/current"
        self.old, self.new = (self.releases / plan[key] for key in ("previous", "new"))
        self.stage = root / "root" / (plan["new"] + "-staging")
        self.backup = root / "root" / ("pre-" + plan["new"])
        self.uv = root / "opt/gremlin-town/toolchain/uv"
        self.wrappers = [root / "usr/local/lib/gremlin-town" / n for n in ("snapshot-stream.sh", "restore.sh")]
        self.watched = [root / "etc/gremlin-town" / n for n in ("config.toml", "participants.toml", "brain-policy.json")] + self.wrappers

    def current_is(self, target: Path) -> None:
        require(self.current.is_symlink() and os.readlink(self.current) == str(target), "current release differs from the checked plan")
        require(target.is_dir() and not target.is_symlink(), "release directory missing or redirected")

    def healthy(self) -> None:
        run(["systemctl", "is-active", "--quiet", "gremlin-town.service"])
        require(ready(), "hub is not ready")

    def receipt(self, name: str, data: dict) -> None:
        with (self.backup / name).open("x", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())

    def preimages(self) -> dict:
        configuration = self.root / "etc/gremlin-town"
        require(configuration.is_dir() and not configuration.is_symlink(), "configuration directory missing or redirected")
        paths = sorted(configuration.rglob("*"))
        require(all(not p.is_symlink() and (p.is_file() or p.is_dir()) for p in paths), "configuration contains a link or special file; inspect before backup")
        images = {}
        # Include credentials and service.env as well as ordinary TOML/policy files.
        # Adding/removing a file or changing access rights invalidates the preimage.
        for path in sorted(set(paths + self.watched + [configuration])):
            stat = path.stat()
            images[str(path.relative_to(self.root))] = {
                "sha256": digest(path) if path.is_file() else None,
                "mode": stat.st_mode, "uid": stat.st_uid, "gid": stat.st_gid,
            }
        images["effective_service_unit"] = hashlib.sha256(run(["systemctl", "cat", "gremlin-town.service"])).hexdigest()
        return images

    def process(self) -> dict[str, str]:
        fields = run(["systemctl", "show", "gremlin-town.service", "--property=MainPID,NRestarts,Restart"]).decode().splitlines()
        state = dict(line.split("=", 1) for line in fields)
        require(state.keys() == {"MainPID", "NRestarts", "Restart"} and int(state["MainPID"]) > 0, "unexpected service process state")
        return state

    def listeners(self) -> list[str]:
        rows = run(["ss", "-H", "-ltn"]).decode().splitlines()
        return sorted(row.split()[3] for row in rows if row.split()[3].rsplit(":", 1)[-1] in {"8765", "8766", "8769"})

    def unchanged(self) -> None:
        saved = json.loads((self.backup / "backup.json").read_text())
        require(saved["plan"] == self.plan and saved["preimages"] == self.preimages(), "configuration or release plan changed since backup")

    def snapshot(self, name: str) -> None:
        with (self.backup / name).open("xb") as output:
            run(["sudo", "-u", "gremlin-town", str(self.wrappers[0])], output=output)
            output.flush()
            os.fsync(output.fileno())
        require((self.backup / name).stat().st_size > 0, "backup stream is empty")

    def backup_step(self) -> None:
        self.current_is(self.old)
        self.healthy()
        require(not self.backup.exists() and not self.backup.is_symlink(), "backup path already exists; do not retry")
        require(not self.new.exists() and not self.new.is_symlink(), "candidate already exists; do not retry")
        before = self.preimages()
        self.backup.mkdir(mode=0o700)
        # Credential backups never leave the root-only hub directory.
        shutil.copytree(self.root / "etc/gremlin-town", self.backup / "configuration", symlinks=True)
        for name, signature in before.items():
            if name.startswith("etc/gremlin-town/") and signature["sha256"] is not None:
                copied = self.backup / "configuration" / Path(name).relative_to("etc/gremlin-town")
                require(digest(copied) == signature["sha256"], "copied configuration differs from checked preimage")
        (self.backup / "effective-service-unit").write_bytes(run(["systemctl", "cat", "gremlin-town.service"]))
        for path in self.wrappers:
            shutil.copy2(path, self.backup / path.name)
        for name in ("gremlin-town", "gremlin-town-brain", "gremlin-town-oauth"):
            backup_tree(self.root / "var/lib" / name, self.backup / name)
        self.snapshot("before.tar")
        require(before == self.preimages(), "configuration changed during backup")
        self.current_is(self.old)
        self.healthy()
        self.receipt("backup.json", {"plan": self.plan, "preimages": before, "listeners": self.listeners()})

    def verify_artifacts(self, directory: Path) -> None:
        require(all(digest(directory / name) == sha for name, sha in self.plan["files"].items()), "artifact hash mismatch")

    def prepare_step(self) -> None:
        self.current_is(self.old)
        self.healthy()
        self.unchanged()
        self.verify_artifacts(self.stage)
        require(not self.new.exists() and not self.new.is_symlink(), "candidate already exists; do not retry")
        self.new.mkdir(mode=0o750)
        artifacts = self.new / "artifact"
        artifacts.mkdir()
        for name in sorted(self.names):
            shutil.copyfile(self.stage / name, artifacts / name)
        self.verify_artifacts(artifacts)
        with tarfile.open(artifacts / "deploy.tar") as archive:
            members = archive.getmembers()
            require(bool(members) and all(
                m.name.startswith("deploy/") and ".." not in Path(m.name).parts
                and "\\" not in m.name and not m.name.startswith("/") and (m.isfile() or m.isdir()) and not m.issparse()
                for m in members
            ) and len({m.name for m in members}) == len(members), "deploy archive contains an unexpected path or link")
            archive.extractall(self.new, filter="data")
        run([str(self.uv), "venv", "--python", str(self.old / ".venv/bin/python"), str(self.new / ".venv")])
        python = str(self.new / ".venv/bin/python")
        run([str(self.uv), "pip", "install", "--python", python, "--require-hashes", "--only-binary=:all:", "-r", str(artifacts / "runtime-requirements.txt")])
        run([str(self.uv), "pip", "install", "--python", python, "--no-deps", *[str(artifacts / n) for n in sorted(self.names) if n.endswith(".whl")]])
        run([str(self.uv), "pip", "check", "--python", python])
        run([python, "-I", "-c", "import brain_core.records, gremlin_town, webauthn, websocket, sys, pathlib, hashlib; p=pathlib.Path(sys.prefix); assert sys.version_info[:2] == (3, 13); assert p == pathlib.Path(sys.argv[1]); assert all(pathlib.Path(m.__file__).is_relative_to(p) for m in (brain_core.records, gremlin_town, webauthn, websocket)); assert hashlib.sha256(pathlib.Path(brain_core.records.__file__).read_bytes()).hexdigest() == sys.argv[2]", str(self.new / ".venv"), self.plan["records_sha256"]])
        for entrypoint in ("brain", "gremlin-town"):
            require((self.new / ".venv/bin" / entrypoint).read_bytes().splitlines()[0] == ("#!" + python).encode(), "entrypoint points outside candidate")
        # Parse only: constructing a runtime could migrate a live store.
        run([python, "-I", "-c", "import tomllib,sys; from gremlin_town.runtime import RUNTIME_CONFIG_SCHEMA; c=tomllib.load(open(sys.argv[1],'rb')); assert set(c)<=set(RUNTIME_CONFIG_SCHEMA); assert all(isinstance(v,dict) and set(v)<=RUNTIME_CONFIG_SCHEMA[k] for k,v in c.items())", str(self.backup / "configuration/config.toml")])
        run([python, "-I", str(self.new / "deploy/validate-backup.py"), "extract", str(self.backup / "before.tar"), str(self.backup / "validated-before")])
        run([str(self.new / ".venv/bin/gremlin-town"), "--help"])
        run(["chown", "-R", "root:gremlin-town", str(self.new)])
        run(["chmod", "-R", "u=rwX,g=rX,o=", str(self.new)])
        self.current_is(self.old)
        self.unchanged()
        self.healthy()
        self.receipt("prepared.json", {"plan": self.plan})

    def prepared(self) -> None:
        require(json.loads((self.backup / "prepared.json").read_text())["plan"] == self.plan, "candidate was not prepared from this plan")
        self.verify_artifacts(self.new / "artifact")

    def switch_step(self) -> None:
        self.current_is(self.old)
        self.healthy()
        self.unchanged()
        self.prepared()
        since = run(["date", "-u", "+%Y-%m-%d %H:%M:%S"]).decode().strip()
        # Intent is written first. A retry refuses even if restart's answer was lost.
        self.receipt("switch-intent.json", {"plan": self.plan})
        temporary = self.current.with_name("mega-current-next")
        require(not temporary.exists() and not temporary.is_symlink(), "temporary link exists")
        temporary.symlink_to(self.new)
        os.replace(temporary, self.current)
        run(["systemctl", "restart", "gremlin-town.service"])
        started = self.process()
        require(started["NRestarts"] == "0", "service automatically restarted; stop for inspection")
        for _ in range(45):
            require(self.process() == started, "service process changed or automatically restarted")
            if ready():
                break
            time.sleep(1)
        else:
            raise Stop("new release did not become ready; current remains on candidate; no rollback attempted")
        self.current_is(self.new)
        self.healthy()
        require(self.process() == started, "service process changed or automatically restarted")
        require(self.listeners() == json.loads((self.backup / "backup.json").read_text())["listeners"], "service listeners differ after switch")
        command = (self.root / "proc" / started["MainPID"] / "cmdline").read_bytes()
        require(str(self.new / ".venv/bin").encode() in command or str(self.current / ".venv/bin").encode() in command, "service process is not running the checked release")
        journal = run(["journalctl", "-u", "gremlin-town.service", "--since", since, "--no-pager", "-o", "cat"])
        require(not re.search(rb"warn|error|traceback", journal, re.I), "service reported a warning or error; inspect privately; no rollback attempted")
        self.receipt("switched.json", {"plan": self.plan})

    def repin_step(self) -> None:
        self.current_is(self.new)
        self.healthy()
        require(json.loads((self.backup / "switched.json").read_text())["plan"] == self.plan, "switch checks have not passed")
        self.unchanged()
        old = f"readonly GT_BACKUP_RELEASE_ROOT={self.old}".encode()
        new = f"readonly GT_BACKUP_RELEASE_ROOT={self.new}".encode()
        changes = []
        for path in self.wrappers:
            original = path.read_bytes()
            require(original.splitlines().count(old) == 1, "backup wrapper pin differs from expected")
            changed = b"\n".join(new if line == old else line for line in original.split(b"\n"))
            changes.append((path, original, changed))
        self.receipt("repin-intent.json", {"plan": self.plan})
        for path, original, changed in changes:
            require(path.read_bytes() == original, "backup wrapper changed before replacement")
            temporary = path.with_name(path.name + ".mega-next")
            with temporary.open("xb") as f:
                f.write(changed)
                f.flush()
                os.fsync(f.fileno())
            shutil.copystat(path, temporary)
            stat = path.stat()
            os.chown(temporary, stat.st_uid, stat.st_gid)
            os.replace(temporary, path)
            require(path.read_bytes() == changed, "backup wrapper replacement differs")
        self.snapshot("after.tar")
        run([str(self.new / ".venv/bin/python"), "-I", str(self.new / "deploy/validate-backup.py"), "extract", str(self.backup / "after.tar"), str(self.backup / "validated-after")])
        self.current_is(self.new)
        self.healthy()
        self.receipt("complete.json", {"plan": self.plan})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("step", choices=("backup", "prepare", "switch", "repin"))
    parser.add_argument("plan", type=Path)
    args = parser.parse_args()
    try:
        require(os.geteuid() == 0, "root required")
        os.umask(0o077)
        release = Release(json.loads(args.plan.read_text()))
        getattr(release, args.step + "_step")()
    except Stop as error:
        print("STOP: " + str(error))
        return 2
    except Exception:
        print("STOP: unexpected local failure; inspect privately; no retry or rollback attempted")
        return 3
    print("PASS: " + args.step + "; stop here and inspect the step log before continuing")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
