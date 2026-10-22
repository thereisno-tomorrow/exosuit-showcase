"""Exercise the toggle against the exact, credential-free hub skill copy; never call Google."""

from __future__ import annotations

import importlib.util
import shutil
import stat
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("gmail_send_toggle", ROOT / "scripts" / "gmail_send_toggle.py")
assert spec is not None and spec.loader is not None
toggle = importlib.util.module_from_spec(spec)
spec.loader.exec_module(toggle)
COPY = ROOT / "tests" / "fixtures" / "hermes_google_workspace" / "google_api.py"


@pytest.fixture
def skill(tmp_path):
    path = tmp_path / "google_api.py"
    shutil.copyfile(COPY, path)
    return path


def load_skill(path, monkeypatch):
    home = types.ModuleType("_hermes_home")
    home.get_hermes_home = lambda: path.parent
    monkeypatch.setitem(sys.modules, "_hermes_home", home)
    monkeypatch.setattr(sys, "path", list(sys.path))
    module = types.ModuleType("copied_google_api")
    module.__file__ = str(path)
    exec(compile(path.read_bytes(), str(path), "exec"), module.__dict__)
    return module


def test_on_off_twice_and_restore_original_bytes(skill):
    original = skill.read_bytes()
    backup = skill.with_name(skill.name + toggle.BACKUP_SUFFIX)
    assert toggle.digest(original) == toggle.KNOWN_SHA256
    assert toggle.toggle("status", skill) == "on"
    assert not backup.exists()
    assert toggle.toggle("on", skill) == "on"
    assert not backup.exists()
    assert toggle.toggle("off", skill) == "off"
    blocked = skill.read_bytes()
    assert blocked != original
    assert backup.read_bytes() == original
    assert toggle.toggle("status", skill) == "off"
    assert toggle.toggle("off", skill) == "off"
    assert skill.read_bytes() == blocked and backup.read_bytes() == original
    assert toggle.toggle("on", skill) == "on"
    assert toggle.toggle("on", skill) == "on"
    assert skill.read_bytes() == original == backup.read_bytes()
    assert toggle.toggle("off", skill) == "off"  # kept backup supports the next toggle


@pytest.mark.parametrize("action", ["status", "off", "on"])
def test_unknown_version_never_changes_file_or_makes_backup(skill, action):
    unknown = skill.read_bytes() + b"\n# newer Hermes\n"
    skill.write_bytes(unknown)
    with pytest.raises(toggle.ToggleError, match="Unknown google_api.py version"):
        toggle.toggle(action, skill)
    assert skill.read_bytes() == unknown
    assert list(skill.parent.iterdir()) == [skill]


@pytest.mark.parametrize("function", ["gmail_send", "gmail_reply"])
@pytest.mark.parametrize("backend", [None, "fake-gws"])
def test_direct_send_and_reply_fail_before_any_backend_or_credentials(skill, monkeypatch, function, backend):
    toggle.toggle("off", skill)
    api = load_skill(skill, monkeypatch)
    monkeypatch.setattr(api, "_gws_binary", lambda: backend)
    monkeypatch.setattr(api, "_run_gws", lambda *a, **kw: pytest.fail("must not call gws"))
    monkeypatch.setattr(api, "build_service", lambda *a: pytest.fail("must not load credentials"))
    with pytest.raises(SystemExit, match="send and reply are OFF"):
        getattr(api, function)(None)


@pytest.mark.parametrize("action", ["send", "reply"])
def test_blocked_commands_are_not_registered(skill, monkeypatch, capsys, action):
    toggle.toggle("off", skill)
    api = load_skill(skill, monkeypatch)
    monkeypatch.setattr(sys, "argv", ["google_api.py", "gmail", action])
    with pytest.raises(SystemExit) as caught:
        api.main()
    assert caught.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_help_keeps_search_read_label_archive_and_restores_send_reply(skill, monkeypatch, capsys):
    toggle.toggle("off", skill)
    monkeypatch.setattr(sys, "argv", ["google_api.py", "gmail", "--help"])
    with pytest.raises(SystemExit) as caught:
        load_skill(skill, monkeypatch).main()
    assert caught.value.code == 0
    help_text = capsys.readouterr().out
    assert "{search,get,labels,modify}" in help_text
    toggle.toggle("on", skill)
    with pytest.raises(SystemExit):
        load_skill(skill, monkeypatch).main()
    assert "{search,get,send,reply,labels,modify}" in capsys.readouterr().out


def test_archive_still_reaches_modify_with_remove_inbox(skill, monkeypatch, capsys):
    toggle.toggle("off", skill)
    api = load_skill(skill, monkeypatch)
    calls = []
    monkeypatch.setattr(api, "_gws_binary", lambda: "fake-gws")

    def fake_gws(command, **kwargs):
        calls.append((command, kwargs))
        return {"id": "message-1", "labelIds": ["STARRED"]}

    monkeypatch.setattr(api, "_run_gws", fake_gws)
    monkeypatch.setattr(sys, "argv", ["google_api.py", "gmail", "modify", "message-1", "--remove-labels", "INBOX"])
    api.main()
    assert calls == [(["gmail", "users", "messages", "modify"],
                     {"params": {"userId": "me", "id": "message-1"},
                      "body": {"removeLabelIds": ["INBOX"]}})]
    assert "STARRED" in capsys.readouterr().out


@pytest.mark.parametrize("action", ["status", "off", "on"])
def test_corrupt_backup_is_refused(skill, action):
    toggle.toggle("off", skill)
    before = skill.read_bytes()
    skill.with_name(skill.name + toggle.BACKUP_SUFFIX).write_bytes(b"bad backup")
    with pytest.raises(toggle.ToggleError, match="Backup is not the known original"):
        toggle.toggle(action, skill)
    assert skill.read_bytes() == before


def test_blocked_file_without_backup_is_refused(skill):
    skill.write_bytes(toggle.blocked_bytes(skill.read_bytes()))
    with pytest.raises(toggle.ToggleError, match="missing original backup"):
        toggle.toggle("on", skill)


def test_a_later_update_is_never_overwritten_from_old_backup(skill):
    toggle.toggle("off", skill)
    changed = skill.read_bytes() + b"\n# update\n"
    skill.write_bytes(changed)
    with pytest.raises(toggle.ToggleError, match="Unknown"):
        toggle.toggle("on", skill)
    assert skill.read_bytes() == changed


def test_file_mode_is_preserved(skill):
    skill.chmod(0o644)
    mode = stat.S_IMODE(skill.stat().st_mode)
    toggle.toggle("off", skill)
    assert stat.S_IMODE(skill.stat().st_mode) == mode
    toggle.toggle("on", skill)
    assert stat.S_IMODE(skill.stat().st_mode) == mode


def test_concurrent_toggle_is_refused(skill):
    before = skill.read_bytes()
    skill.with_name(skill.name + ".gmail-send-lock").write_bytes(b"")
    with pytest.raises(toggle.ToggleError, match="Toggle lock exists"):
        toggle.toggle("off", skill)
    assert skill.read_bytes() == before


def test_cli_prints_state_and_clear_unknown_refusal(skill, capsys):
    assert toggle.main(["off", "--skill-file", str(skill)]) == 0
    assert "Gmail send/reply: OFF" in capsys.readouterr().out
    skill.write_bytes(b"unknown")
    assert toggle.main(["on", "--skill-file", str(skill)]) == 1
    assert "Unknown google_api.py version" in capsys.readouterr().err
