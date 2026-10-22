from __future__ import annotations

import hashlib
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import sysconfig
import tarfile
import venv
from contextlib import closing
from pathlib import Path

import pytest

from scripts.build_release import build
from gremlin_town.storage.migrations import MIGRATIONS


ROOT = Path(__file__).resolve().parents[2]


def test_release_tree_manifest_archive_and_installer_policy_agree(tmp_path: Path) -> None:
    tree, archive = build(ROOT, tmp_path, "packaging-contract")

    files = {
        path.relative_to(tree).as_posix()
        for path in tree.rglob("*")
        if path.is_file()
    }
    assert {path.split("/", 1)[0] for path in files} == {
        "RELEASE-MANIFEST.sha256",
        "adapters",
        "deploy",
        "docs",
        "pyproject.toml",
        "src",
        "uv.lock",
    }
    assert not any(path.startswith("tests/") for path in files)
    assert "deploy/service.env.example" in files
    assert {"deploy/validate-backup.py", "deploy/sqlite_backup.py", "deploy/snapshot-stream.sh", "deploy/restore.sh"} <= files

    manifest: dict[str, str] = {}
    for line in (tree / "RELEASE-MANIFEST.sha256").read_text(encoding="utf-8").splitlines():
        digest, relative = line.split("  ", 1)
        manifest[relative] = digest
    expected_files = files - {"RELEASE-MANIFEST.sha256"}
    assert set(manifest) == expected_files
    for relative, digest in manifest.items():
        assert hashlib.sha256((tree / relative).read_bytes()).hexdigest() == digest

    with tarfile.open(archive, "r:gz") as bundle:
        archived = {
            member.name.split("/", 1)[1]
            for member in bundle.getmembers()
            if member.isfile() and "/" in member.name
        }
    assert archived == files

    apply_script = (tree / "deploy" / "apply.sh").read_text(encoding="utf-8")
    assert "deploy/service.env.example) ;;" in apply_script
    assert "|deploy|deploy/*|docs|docs/*) ;;" in apply_script
    assert 'installed_release="/opt/gremlin-town/releases/$release_id"' in apply_script
    assert '"$installed_release/deploy/gremlin-town.service"' in apply_script
    assert 'render_backup_wrapper "$installed_release/deploy/$wrapper" "$installed_release"' in apply_script


def test_pc_restore_protects_new_plaintext_target_before_decryption() -> None:
    script = (ROOT / "deploy" / "restore-from-pc.ps1").read_text(encoding="utf-8")
    create_at = script.index("New-Item -ItemType Directory -Path $target")
    acl_at = script.index("icacls.exe $target /inheritance:r")
    decrypt_at = script.index("& $Restic restore $SnapshotId --target $target")

    assert create_at < acl_at < decrypt_at
    assert "${currentIdentity}:(OI)(CI)F" in script
    assert "failed to restrict restore ACL" in script


def _bash() -> str:
    if os.name == "nt":
        path = Path("C:/Program Files/Git/bin/bash.exe")
        if path.exists():
            return str(path)
    path = shutil.which("bash")
    if path is None:
        pytest.skip("Bash required for installed backup wrapper dependency test")
    return path


def _run_bash(script: str, *arguments: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [_bash(), "--noprofile", "--norc", "-c", "set -euo pipefail\n" + script, "test", *(path.as_posix() for path in arguments)],
        text=True, capture_output=True, check=False,
    )
    if check:
        assert result.returncode == 0, result.stderr
    return result


def _pointer(link: Path, destination: Path) -> None:
    if os.name == "nt":
        import _winapi
        _winapi.CreateJunction(str(destination), str(link))
    else:
        link.symlink_to(destination, target_is_directory=True)


@pytest.mark.parametrize("schema_version", [1, 2])
def test_installed_backup_dependencies_survive_pre_mcp_pointer_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, schema_version: int,
) -> None:
    release, _ = build(ROOT, tmp_path, "pinned-backup")
    old_release = tmp_path / "old-release"
    (old_release / "deploy").mkdir(parents=True)
    # Frozen pre-MCP configuration shape, checked against the production-build
    # baseline. Keep this fixture independent of ignored local baseline files
    # and of the current config example (which now contains MCP fields).
    (old_release / "deploy/config.toml").write_text('''[server]
host = "127.0.0.1"
port = 8765
database = "/var/lib/gremlin-town/gremlin-town.sqlite3"
operator_only = false
[agentmail]
base_url = "https://api.agentmail.to"
inbox_id = "fake@example.test"
api_key_file_env = "GT_AGENTMAIL_API_KEY_FILE"
[recovery]
normal_clients_allowed = true
outbound_dispatch_allowed = true
[participants]
file = "/etc/gremlin-town/participants.toml"
''', encoding="ascii")
    current = tmp_path / "current"
    _pointer(current, release)

    # A disposable venv models the installed release. Its trusted site setup
    # selects packaged sources plus the clean interpreter's installed libraries;
    # no caller environment is needed to find the matching allowlists/migrations.
    environment = release / ".venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    if os.name == "nt":
        real_interpreter = environment / "Scripts/python.exe"
        site = environment / "Lib/site-packages"
        interpreter = environment / "bin/python"
        interpreter.parent.mkdir()
        interpreter.write_text(
            "#!/usr/bin/env bash\n"
            f"exec {shlex.quote(real_interpreter.as_posix())} \"$@\"\n",
            encoding="utf-8", newline="\n",
        )
        interpreter.chmod(0o755)
    else:
        real_interpreter = environment / "bin/python"
        site = environment / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages"
    (site / "test-release.pth").write_text(
        (release / "src").as_posix() + "\n" + Path(sysconfig.get_path("purelib")).as_posix() + "\n",
        encoding="utf-8",
    )
    poison = tmp_path / "poisoned-pythonpath"
    (poison / "gremlin_town").mkdir(parents=True)
    for module in (poison / "sqlite3.py", poison / "gremlin_town/__init__.py"):
        module.write_text('raise RuntimeError("POISONED_PYTHONPATH_LOADED")\n', encoding="ascii")
    monkeypatch.setenv("PYTHONPATH", str(poison))
    # Prove the poison is active without isolation, rather than accepting an
    # ineffective fixture. All real wrapper invocations below must ignore it.
    for name in ("sqlite3", "gremlin_town"):
        probe = subprocess.run([str(real_interpreter), "-c", f"import {name}"], text=True, capture_output=True)
        assert probe.returncode != 0 and "POISONED_PYTHONPATH_LOADED" in probe.stderr
    isolated = subprocess.run(
        [str(real_interpreter), "-I", "-c", "import gremlin_town.runtime; print(gremlin_town.runtime.__file__)"],
        text=True, capture_output=True,
    )
    assert isolated.returncode == 0, isolated.stderr
    assert Path(isolated.stdout.strip()).resolve().is_relative_to(release / "src")
    installed = tmp_path / "installed-wrappers"
    installed.mkdir()
    # Run the installer's real pure renderer, excluding all privileged apply
    # effects. Neither the renderer nor the dependency commands are reimplemented.
    renderer = (release / "deploy/apply.sh").read_text().split("\nfail()", 1)[0]
    for name in ("snapshot-stream.sh", "restore.sh"):
        _run_bash(renderer + '\nrender_backup_wrapper "$1" "$2" > "$3"\n',
                  release / "deploy" / name, release, installed / name)

    # Only remove the disposable link itself; preserve both release directories.
    if os.name == "nt":
        current.rmdir()
    else:
        current.unlink()
    _pointer(current, old_release)
    assert not (current / "deploy/validate-backup.py").exists()

    data = tmp_path / "actual-work"
    data.mkdir()
    database = data / "live.sqlite3"
    with closing(sqlite3.connect(database)) as db:
        for version in range(1, schema_version + 1):
            # Historical migrations 1/2 predate MCP; no current store migration.
            db.executescript(MIGRATIONS[version])
        db.execute(f"PRAGMA user_version={schema_version}")
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    staging = tmp_path / "backup-staging"
    staging.mkdir()
    shutil.copyfile(current / "deploy/config.toml", staging / "config.toml")
    participants = ('[[participant]]\nid = "fake-operator"\nrole = "operator"\n'
                    f'token_sha256 = "{hashlib.sha256(b"fake-test-credential").hexdigest()}"\n')
    (staging / "participants.toml").write_text(participants, encoding="utf-8")
    (staging / "MANIFEST").write_text("release=pre-mcp-rollback\n", encoding="ascii")

    def dependency_script(name: str) -> tuple[str, list[str]]:
        script = (installed / name).read_text()
        assignment = next(line for line in script.splitlines() if line.startswith("readonly GT_BACKUP_RELEASE_ROOT="))
        commands = re.findall(r'^"\$GT_BACKUP_RELEASE_ROOT/\.venv/bin/python" -I \\\n  [^\n]+', script, re.MULTILINE)
        assert len(commands) == (2 if name == "snapshot-stream.sh" else 1)
        assert "/current/" not in "\n".join(commands)
        # A hostile caller-provided root must not replace the installed pin.
        return 'export GT_BACKUP_RELEASE_ROOT="$1"\n' + assignment + "\n", commands

    prefix, snapshot_commands = dependency_script("snapshot-stream.sh")
    copied = _run_bash(prefix + 'db="$2"\nrun_dir="$3"\n' + snapshot_commands[0], current, database, staging, check=False)
    if os.name == "nt" and copied.returncode != 0:
        # The existing Linux-only helper fsyncs a read-only fd, which Windows
        # rejects after its real SQLite backup/integrity check. Do not shim away
        # fsync or claim Linux durability was verified here. Still exercise the
        # produced snapshot through both real pinned validator invocations.
        assert "os.fsync(handle.fileno())" in copied.stderr
        assert copied.stderr.rstrip().endswith("OSError: [Errno 9] Bad file descriptor")
    else:
        assert copied.returncode == 0, copied.stderr
    (staging / "SHA256SUMS").write_text("".join(
        f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
        for path in sorted(staging.iterdir())
    ), encoding="ascii")
    _run_bash(prefix + 'run_dir="$2"\n' + snapshot_commands[1], current, staging)
    archive = tmp_path / "work.tar"
    with tarfile.open(archive, "w", format=tarfile.PAX_FORMAT) as bundle:
        for path in staging.iterdir():
            bundle.add(path, arcname=path.name)
    archive_before = hashlib.sha256(archive.read_bytes()).hexdigest()
    prefix, restore_commands = dependency_script("restore.sh")
    target = tmp_path / "restored"
    _run_bash(prefix + 'archive="$2"\ntarget="$3"\n' + restore_commands[0], current, archive, target)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert hashlib.sha256(archive.read_bytes()).hexdigest() == archive_before
    assert {path.name: path.read_bytes() for path in target.iterdir()} == {
        path.name: path.read_bytes() for path in staging.iterdir()
    }
    assert not (current / "deploy/validate-backup.py").exists()
