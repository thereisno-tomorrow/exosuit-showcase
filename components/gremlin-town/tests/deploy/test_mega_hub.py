"""Release safety checks with disposable files and fake commands; no hub/network."""
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import tarfile

import pytest


SPEC = importlib.util.spec_from_file_location("mega_hub", Path(__file__).parents[2] / "deploy/release/mega-hub.py")
hub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hub)


@pytest.fixture
def release(tmp_path, monkeypatch):
    plan = {"new": "20261002-mega-01", "previous": "20261001-work-02", "commit": "a" * 40,
            "records_sha256": "c" * 64,
            "files": {name: "b" * 64 for name in ("brain_core-0.4.0-py3-none-any.whl", "gremlin_town-0.1.0-py3-none-any.whl", "runtime-requirements.txt", "deploy.tar")}}
    result = hub.Release(plan, tmp_path)
    result.backup.mkdir(parents=True)
    result.old.mkdir(parents=True)
    result.stage.mkdir(parents=True)
    monkeypatch.setattr(result, "current_is", lambda _: None)
    monkeypatch.setattr(result, "healthy", lambda: None)
    monkeypatch.setattr(result, "unchanged", lambda: None)
    monkeypatch.setattr(result, "prepared", lambda: None)
    monkeypatch.setattr(hub, "ready", lambda: True)
    return result


def test_database_backup_captures_committed_wal(tmp_path):
    source = tmp_path / "live"
    source.mkdir()
    connection = sqlite3.connect(source / "state.sqlite3")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("create table checks (value text)")
    connection.execute("insert into checks values ('fixture-only')")
    connection.commit()
    assert (source / "state.sqlite3-wal").exists()
    hub.backup_tree(source, tmp_path / "retained")
    with sqlite3.connect(tmp_path / "retained/state.sqlite3") as restored:
        assert restored.execute("select value from checks").fetchall() == [("fixture-only",)]
    assert not (tmp_path / "retained/state.sqlite3-wal").exists()
    connection.close()


def test_orphan_sidecars_are_preserved_as_stable_raw_files(tmp_path):
    source = tmp_path / "live"
    source.mkdir()
    expected = {"retired.sqlite3-shm": b"s" * 32768, "retired.sqlite3-wal": b""}
    for name, data in expected.items():
        (source / name).write_bytes(data)
    hub.backup_tree(source, tmp_path / "retained")
    assert not (source / "retired.sqlite3").exists()
    assert not (tmp_path / "retained/retired.sqlite3").exists()
    for name, data in expected.items():
        assert (source / name).read_bytes() == data
        assert (tmp_path / "retained" / name).read_bytes() == data


def test_orphan_with_sqlite_header_is_still_retained_raw(tmp_path, monkeypatch):
    source = tmp_path / "live"
    source.mkdir()
    raw = b"SQLite format 3\x00not an actual database"
    (source / "orphan-wal").write_bytes(raw)
    monkeypatch.setattr(hub.sqlite3, "connect", lambda *a, **k: pytest.fail("orphan must not be opened as a database"))
    hub.backup_tree(source, tmp_path / "retained")
    assert (tmp_path / "retained/orphan-wal").read_bytes() == raw


@pytest.mark.parametrize("change", ["mutate", "remove", "base_appears"])
def test_orphan_change_during_copy_refuses_and_keeps_partial_backup(tmp_path, monkeypatch, change):
    source = tmp_path / "live"
    source.mkdir()
    orphan = source / "retired.sqlite3-wal"
    orphan.write_bytes(b"fixture")
    original = hub.shutil.copyfile
    def copy_then_change(incoming, outgoing):
        original(incoming, outgoing)
        if change == "mutate":
            orphan.write_bytes(b"changed fixture")
        elif change == "remove":
            orphan.unlink()
        else:
            (source / "retired.sqlite3").write_bytes(b"new fixture")
    monkeypatch.setattr(hub.shutil, "copyfile", copy_then_change)
    with pytest.raises(hub.Stop, match="changed|missing|appeared"):
        hub.backup_tree(source, tmp_path / "retained")
    assert (tmp_path / "retained/retired.sqlite3-wal").read_bytes() == b"fixture"


@pytest.mark.parametrize("change", ["base_appears", "mutate", "remove"])
def test_later_sibling_copy_cannot_hide_orphan_change(tmp_path, monkeypatch, change):
    source = tmp_path / "live"
    source.mkdir()
    orphan = source / "old-wal"
    orphan.write_bytes(b"orphan fixture")
    sibling = source / "zz-other"
    sibling.write_bytes(b"sibling fixture")
    original = hub.shutil.copyfile
    def copy_then_change(incoming, outgoing):
        original(incoming, outgoing)
        if incoming == sibling:
            if change == "base_appears":
                (source / "old").write_bytes(b"new base fixture")
            elif change == "mutate":
                orphan.write_bytes(b"changed orphan fixture")
            else:
                orphan.unlink()
    monkeypatch.setattr(hub.shutil, "copyfile", copy_then_change)
    with pytest.raises(hub.Stop, match="appeared|changed|missing"):
        hub.backup_tree(source, tmp_path / "retained")
    assert (tmp_path / "retained/old-wal").read_bytes() == b"orphan fixture"


@pytest.mark.parametrize("linked", ["sidecar", "base"])
def test_sidecar_or_base_link_refuses_before_copy(tmp_path, monkeypatch, linked):
    source = tmp_path / "live"
    source.mkdir()
    sidecar = source / "state.sqlite3-wal"
    sidecar.write_bytes(b"fixture")
    base = source / "state.sqlite3"
    real = Path.is_symlink
    target = sidecar if linked == "sidecar" else base
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == target or real(path))
    with pytest.raises(hub.Stop, match="link"):
        hub.backup_tree(source, tmp_path / "retained")
    assert not (tmp_path / "retained" / sidecar.name).exists()


def test_sidecar_with_non_sqlite_base_still_refuses(tmp_path):
    source = tmp_path / "live"
    source.mkdir()
    (source / "state").write_bytes(b"not a database")
    (source / "state-wal").write_bytes(b"fixture")
    with pytest.raises(hub.Stop, match="unrecognized SQLite sidecar"):
        hub.backup_tree(source, tmp_path / "retained")


def test_paired_database_disappearing_after_snapshot_is_not_an_orphan(tmp_path, monkeypatch):
    source = tmp_path / "live"
    source.mkdir()
    base = source / "state.sqlite3"
    connection = sqlite3.connect(base)
    connection.execute("CREATE TABLE fixture (value TEXT)")
    connection.commit()
    connection.close()
    (source / "state.sqlite3-wal").write_bytes(b"")
    original = Path.exists
    disappeared = []
    def remove_after_database_copy(path):
        if path == base and not disappeared:
            assert (tmp_path / "retained/state.sqlite3").is_file()
            base.unlink()
            disappeared.append(True)
        return original(path)
    monkeypatch.setattr(Path, "exists", remove_after_database_copy)
    with pytest.raises(hub.Stop, match="disappeared"):
        hub.backup_tree(source, tmp_path / "retained")
    assert not (tmp_path / "retained/state.sqlite3-wal").exists()


def test_initial_orphan_base_appearing_before_its_turn_refuses(tmp_path, monkeypatch):
    source = tmp_path / "live"
    source.mkdir()
    earlier = source / "a-first"
    earlier.write_bytes(b"earlier fixture")
    (source / "orphan-wal").write_bytes(b"")
    original = hub.shutil.copyfile
    def create_base_while_copying_earlier_file(incoming, outgoing):
        original(incoming, outgoing)
        if incoming == earlier:
            (source / "orphan").write_bytes(b"SQLite format 3\x00new base fixture")
    monkeypatch.setattr(hub.shutil, "copyfile", create_base_while_copying_earlier_file)
    with pytest.raises(hub.Stop, match="appeared"):
        hub.backup_tree(source, tmp_path / "retained")
    assert not (tmp_path / "retained/orphan-wal").exists()


def test_command_failure_never_repeats_or_echoes_private_output(monkeypatch, capsys):
    calls = []
    class Result:
        returncode = 1
        stdout = b"private fixture response"
        stderr = b"private fixture error"
    monkeypatch.setattr(hub.subprocess, "run", lambda *args, **kwargs: calls.append(args) or Result())
    with pytest.raises(hub.Stop) as error:
        hub.run(["fixture", "private fixture argument"])
    assert len(calls) == 1
    assert "private fixture" not in str(error.value)
    assert capsys.readouterr().out == ""


def test_bad_artifact_hash_refuses_before_candidate_creation(release, monkeypatch):
    for name in release.names:
        (release.stage / name).write_bytes(b"fixture")
    monkeypatch.setattr(hub, "run", lambda *a, **k: pytest.fail("must not run a command"))
    with pytest.raises(hub.Stop, match="hash mismatch"):
        release.prepare_step()
    assert not release.new.exists()


@pytest.mark.parametrize("name,kind", [("deploy/../outside", "file"), ("deploy/link", "link"), ("deploy/duplicate", "duplicate"), ("deploy\\outside", "file")])
def test_unsafe_deploy_archive_never_installs(release, monkeypatch, name, kind):
    for filename in release.names - {"deploy.tar"}:
        (release.stage / filename).write_bytes(b"fixture")
    with tarfile.open(release.stage / "deploy.tar", "w") as archive:
        info = tarfile.TarInfo(name)
        if kind == "link":
            info.type, info.linkname = tarfile.SYMTYPE, "/outside"
        archive.addfile(info, io.BytesIO())
        if kind == "duplicate":
            archive.addfile(info, io.BytesIO())
    release.plan["files"] = {n: hub.digest(release.stage / n) for n in release.names}
    monkeypatch.setattr(hub, "run", lambda *a, **k: pytest.fail("must not install"))
    with pytest.raises(hub.Stop, match="archive"):
        release.prepare_step()
    assert not (release.new / "outside").exists()


def prepare_switch(release, monkeypatch):
    calls = []
    release.current.write_text("previous fixture")
    monkeypatch.setattr(Path, "symlink_to", lambda path, target: path.write_text(str(target)))
    monkeypatch.setattr(hub.time, "sleep", lambda _: None)
    monkeypatch.setattr(release, "process", lambda: {"MainPID": "123", "NRestarts": "0", "Restart": "on-failure"})
    def run(args, **kwargs):
        calls.append(args)
        return b"2026-10-02 12:00:00" if args[0] == "date" else b""
    monkeypatch.setattr(hub, "run", run)
    return calls


def test_failed_readiness_leaves_candidate_and_never_restarts_twice(release, monkeypatch):
    calls = prepare_switch(release, monkeypatch)
    monkeypatch.setattr(hub, "ready", lambda: False)
    with pytest.raises(hub.Stop, match="no rollback"):
        release.switch_step()
    assert release.current.read_text() == str(release.new)
    assert [c for c in calls if "restart" in c] == [["systemctl", "restart", "gremlin-town.service"]]
    assert (release.backup / "switch-intent.json").exists()
    assert not (release.backup / "switched.json").exists()
    with pytest.raises(FileExistsError):
        release.switch_step()
    assert len([c for c in calls if "restart" in c]) == 1


def test_auto_restart_is_a_surprise_even_if_ready(release, monkeypatch):
    calls = prepare_switch(release, monkeypatch)
    monkeypatch.setattr(release, "process", lambda: {"MainPID": "123", "NRestarts": "1", "Restart": "on-failure"})
    with pytest.raises(hub.Stop, match="automatically restarted"):
        release.switch_step()
    assert len([c for c in calls if "restart" in c]) == 1


def test_changed_preimage_stops_before_switch(release, monkeypatch):
    calls = prepare_switch(release, monkeypatch)
    def changed():
        raise hub.Stop("configuration changed")
    monkeypatch.setattr(release, "unchanged", changed)
    with pytest.raises(hub.Stop, match="configuration changed"):
        release.switch_step()
    assert release.current.read_text() == "previous fixture"
    assert calls == []


def test_repin_changes_only_exact_assignment_and_retains_backup(release, monkeypatch):
    originals = {}
    for path in release.wrappers:
        path.parent.mkdir(parents=True, exist_ok=True)
        original = (f"#!/bin/bash\nreadonly GT_BACKUP_RELEASE_ROOT={release.old}\nprintf fixture\n").encode()
        path.write_bytes(original)
        (release.backup / path.name).write_bytes(original)
        originals[path] = original
    (release.backup / "switched.json").write_text(json.dumps({"plan": release.plan}))
    monkeypatch.setattr(hub.os, "chown", lambda *args: None, raising=False)
    snapshots = []
    monkeypatch.setattr(release, "snapshot", snapshots.append)
    monkeypatch.setattr(hub, "run", lambda *a, **k: b"")
    release.repin_step()
    for path in release.wrappers:
        assert path.read_bytes() == originals[path].replace(str(release.old).encode(), str(release.new).encode())
        assert (release.backup / path.name).read_bytes() == originals[path]
    assert snapshots == ["after.tar"]
    assert (release.backup / "complete.json").exists()


def test_plan_cannot_escape_release_roots(release):
    invalid = {**release.plan, "new": "../../outside"}
    with pytest.raises(hub.Stop, match="invalid release"):
        hub.Release(invalid)


def make_configuration(release, monkeypatch):
    for path in release.watched:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    env = release.root / "etc/gremlin-town/service.env"
    env.write_text("FIXTURE=one")
    monkeypatch.setattr(hub, "run", lambda *a, **k: b"fixture service")
    return env


def test_service_environment_change_invalidates_backup(release, monkeypatch):
    env = make_configuration(release, monkeypatch)
    before = release.preimages()
    (release.backup / "backup.json").write_text(json.dumps({"plan": release.plan, "preimages": before}))
    env.write_text("FIXTURE=two")
    with pytest.raises(hub.Stop, match="configuration"):
        hub.Release.unchanged(release)


def test_configuration_inventory_changes_invalidate_backup(release, monkeypatch):
    env = make_configuration(release, monkeypatch)
    before = release.preimages()
    (release.backup / "backup.json").write_text(json.dumps({"plan": release.plan, "preimages": before}))
    env.with_name("new-credential").write_text("synthetic credential")
    with pytest.raises(hub.Stop, match="configuration"):
        hub.Release.unchanged(release)


def test_configuration_link_refuses_before_copy(release, monkeypatch):
    env = make_configuration(release, monkeypatch)
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda p: p == env or real(p))
    with pytest.raises(hub.Stop, match="link"):
        release.preimages()


def test_new_release_name_preserves_failed_attempt_and_binds_receipts(release, monkeypatch):
    make_configuration(release, monkeypatch)
    old_partial = release.backup / "partial-state"
    old_partial.write_bytes(b"failed attempt retained")
    old_staged = release.stage / "plan.json"
    old_staged.write_text(json.dumps(release.plan))
    old_plan = dict(release.plan)
    for name in ("gremlin-town", "gremlin-town-brain", "gremlin-town-oauth"):
        source = release.root / "var/lib" / name
        source.mkdir(parents=True)
        (source / "fixture").write_bytes(b"state")
    fresh = hub.Release({**old_plan, "new": "20261002-mega-02", "commit": "d" * 40}, release.root)
    monkeypatch.setattr(fresh, "current_is", lambda _: None)
    monkeypatch.setattr(fresh, "healthy", lambda: None)
    monkeypatch.setattr(fresh, "listeners", lambda: ["127.0.0.1:8765"])
    monkeypatch.setattr(fresh, "snapshot", lambda name: (fresh.backup / name).write_bytes(b"fixture snapshot"))
    assert fresh.stage != release.stage and fresh.new != release.new and fresh.backup != release.backup
    fresh.backup_step()
    assert old_partial.read_bytes() == b"failed attempt retained"
    assert json.loads(old_staged.read_text()) == old_plan
    assert not (release.backup / "backup.json").exists()
    assert json.loads((fresh.backup / "backup.json").read_text())["plan"] == fresh.plan
    fresh.unchanged()
    with pytest.raises(hub.Stop, match="already exists; do not retry"):
        fresh.backup_step()
    # A prior attempt's receipt cannot authorize prepare/switch for this plan.
    receipt = json.loads((fresh.backup / "backup.json").read_text())
    receipt["plan"] = old_plan
    (fresh.backup / "backup.json").write_text(json.dumps(receipt))
    with pytest.raises(hub.Stop, match="release plan changed"):
        fresh.unchanged()


@pytest.mark.parametrize("method,receipt_name,message", [
    ("prepared", "prepared.json", "not prepared from this plan"),
    ("repin_step", "switched.json", "switch checks have not passed"),
])
def test_fresh_release_rejects_other_attempt_later_receipts(release, monkeypatch, method, receipt_name, message):
    (release.backup / receipt_name).write_text(json.dumps({"plan": {**release.plan, "new": "20261002-mega-02"}}))
    monkeypatch.setattr(hub, "run", lambda *a, **k: pytest.fail("must not mutate"))
    # Fixture mocks prepared for switch tests; call its real method here.
    with pytest.raises(hub.Stop, match=message):
        getattr(hub.Release, method)(release)


@pytest.mark.parametrize("name", ["/root/elsewhere", "20261002-mega/../../elsewhere", "20261002-mega\\elsewhere", "../20261002-mega-02"])
def test_fresh_release_name_cannot_redirect_backup(release, name):
    with pytest.raises(hub.Stop, match="invalid release name"):
        hub.Release({**release.plan, "new": name}, release.root)
