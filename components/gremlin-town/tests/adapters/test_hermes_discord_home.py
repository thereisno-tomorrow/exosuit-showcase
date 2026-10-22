"""E23: synthetic mail, native CLI fakes, and temporary config only."""
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from test_hermes_mail_monitor import FakeGmail, FakeScorer, LOGIN, NOW, mm

SCRIPT = Path(__file__).resolve().parents[2] / "deploy/discord-home/install.py"
spec = importlib.util.spec_from_file_location("discord_home_install", SCRIPT)
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.delenv("MAIL_MONITOR_TOKEN_PATH", raising=False)
    (tmp_path / "google_token_mail.json").write_text(json.dumps(LOGIN))
    return tmp_path


def route_file(home, **changes):
    path = home / "mail-monitor/delivery.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"version": 1, "enabled": True, "mail_pings": True,
                                "home_channel_id": "12345", **changes}))
    return path


def state(home):
    path = home / "mail-monitor/state.json"
    mm.save_state(path, {"cursor": NOW - 60, "seen": {}})
    return path


def test_default_off_keeps_telegram_without_subprocess(home):
    assert mm.delivery_route(home) is None
    route_file(home, enabled=False, mail_pings=False, home_channel_id="")
    assert mm.delivery_route(home) is None
    assert mm.route_ping("a ping", None, runner=lambda *a, **k: pytest.fail("CLI called")) == "a ping"


@pytest.mark.parametrize("changes", [{"enabled": "true"}, {"mail_pings": 1},
    {"enabled": False}, {"version": True}, {"home_channel_id": "123;run"}, {"extra": "x"}])
def test_invalid_route_refused_before_mail_or_cursor(home, changes, monkeypatch, capsys):
    route_file(home, **changes)
    before = state(home).read_bytes()
    monkeypatch.setattr(mm, "hermes_home", lambda: home)
    monkeypatch.setattr(mm, "run", lambda *a, **k: pytest.fail("mail was read"))
    assert mm.main([]) == 1
    assert state_content(home) == before
    assert "settings need checking" in capsys.readouterr().err


def state_content(home):
    return (home / "mail-monitor/state.json").read_bytes()


def test_native_send_uses_stdin_and_suppresses_mentions(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "unrelated-profile")
    calls = []
    def runner(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout='{"success": true, "message_id": "synthetic"}')
    body = "@everyone @here <@123> <@&456>\n" + mm.FOOTER
    assert mm.route_ping(body, "discord:12345", home=tmp_path, runner=runner) == ""
    args, kwargs = calls[0]
    assert args == ["hermes", "send", "--to", "discord:12345", "--json", "--file", "-"]
    assert body not in args and kwargs["timeout"] == 30
    assert kwargs["env"]["HERMES_HOME"] == str(tmp_path.resolve())
    assert "@everyone" not in kwargs["input"] and "<@123>" not in kwargs["input"]
    assert mm.DISCORD_FOOTER in kwargs["input"]


@pytest.mark.parametrize("result", [
    SimpleNamespace(returncode=1, stdout='{"success":true,"error":"private synthetic detail"}'),
    SimpleNamespace(returncode=0, stdout='{"success":false}'),
    SimpleNamespace(returncode=0, stdout='{"success":1}'),
    SimpleNamespace(returncode=0, stdout="private synthetic detail"),
    SimpleNamespace(returncode=0, stdout="[]"),
    OSError("private synthetic detail"), subprocess.TimeoutExpired("private synthetic detail", 30),
])
def test_unconfirmed_send_returns_explicit_telegram_fallback_without_cli_output(result, capsys):
    def runner(*args, **kwargs):
        if isinstance(result, Exception):
            raise result
        return result
    output = mm.route_ping("synthetic ping", "discord:12345", runner=runner)
    assert output == mm.TELEGRAM_FALLBACK + "\n\nsynthetic ping"
    assert "private synthetic detail" not in output
    assert capsys.readouterr() == ("", "")


def test_confirmed_send_happens_before_cursor_advances(home):
    path = state(home)
    before = path.read_bytes()
    calls = []
    def deliver(body):
        assert path.read_bytes() == before
        calls.append(body)
        return ""
    assert mm.run(home, http=FakeGmail(["mail-a"]), scorer=FakeScorer(default=8),
                  now=lambda: NOW, deliver=deliver) == ""
    assert len(calls) == 1 and "Important email" in calls[0]
    assert json.loads(path.read_text())["seen"] == {"mail-a": NOW}


def test_delivery_exception_leaves_cursor_unchanged(home):
    path = state(home)
    before = path.read_bytes()
    def fail(body):
        raise mm.MonitorError("Synthetic delivery failure")
    with pytest.raises(mm.MonitorError):
        mm.run(home, http=FakeGmail(["mail-a"]), scorer=FakeScorer(default=8),
               now=lambda: NOW, deliver=fail)
    assert path.read_bytes() == before


def test_telegram_fallback_preserves_existing_stdout_handoff(home):
    path = state(home)
    output = mm.run(home, http=FakeGmail(["mail-a"]), scorer=FakeScorer(default=8),
        now=lambda: NOW, deliver=lambda body: mm.route_ping(body, "discord:12345",
            runner=lambda *a, **k: SimpleNamespace(returncode=1, stdout="{}")))
    assert output.startswith(mm.TELEGRAM_FALLBACK)
    assert "Subject mail-a" in output and mm.FOOTER in output
    assert json.loads(path.read_text())["cursor"] == NOW


def test_quiet_run_never_sends(home):
    state(home)
    assert mm.run(home, http=FakeGmail(), scorer=FakeScorer(), now=lambda: NOW,
                  deliver=lambda body: pytest.fail("quiet run sent")) == ""


def test_first_run_delivery_failure_does_not_create_cursor(home):
    def fail(body):
        raise mm.MonitorError("Synthetic delivery failure")
    with pytest.raises(mm.MonitorError):
        mm.run(home, http=FakeGmail(), now=lambda: NOW, deliver=fail)
    assert not (home / "mail-monitor/state.json").exists()


@pytest.fixture
def profile(tmp_path):
    home = tmp_path / "profile"
    home.mkdir()
    config = tmp_path / "gt.toml"
    config.write_text('[discord]\nenabled=true\nmail_pings=true\ndiablo_home=true\nhome_channel_id="12345"\n')
    (home / ".env").write_bytes(b"# preserved\nTELEGRAM_HOME_CHANNEL=synthetic\nDISCORD_BOT_TOKEN=synthetic\nOTHER=keep\n")
    route = {"events": ["gt.request.ready"], "secret": "synthetic", "prompt": "Synthetic prompt",
             "skills": ["gremlin-town"], "deliver": "telegram", "enabled": True,
             "toolsets": ["hermes-telegram"]}
    (home / "webhook_subscriptions.json").write_text(json.dumps({"gremlin-town-wake": route,
                                                               "other": {"keep": True}}))
    return home, config, tmp_path / "plan.json"


@pytest.mark.parametrize("action", ["mail-pings", "home-channel", "wake-route"])
def test_prepare_contains_no_private_values_and_changes_nothing(profile, action):
    home, config, plan = profile
    before = {p.name: p.read_bytes() for p in home.iterdir()}
    assert installer.prepare(home, config, action, plan)
    assert {p.name: p.read_bytes() for p in home.iterdir()} == before
    text = plan.read_text()
    assert "12345" not in text and "synthetic" not in text and "Synthetic prompt" not in text
    with pytest.raises(FileExistsError):
        installer.prepare(home, config, action, plan)


@pytest.mark.parametrize("action", ["mail-pings", "home-channel", "wake-route"])
def test_apply_backs_up_checks_and_preserves_unrelated_configuration(profile, action):
    home, config, plan = profile
    target = home / installer.ACTIONS[action]
    before = target.read_bytes() if target.exists() else None
    expected, _, _, after = installer.snapshot(home, config, action)
    installer.prepare(home, config, action, plan)
    assert installer.apply(home, config, action, plan)
    assert target.read_bytes() == after
    backups = list((home / "discord-home-backups").iterdir())
    assert len(backups) == 1
    assert (backups[0] / "before").read_bytes() == (before or b"")
    assert (backups[0] / "applied").read_bytes() == b"checked\n"
    if action == "home-channel":
        assert target.read_bytes() == before + b"DISCORD_HOME_CHANNEL=12345\n"
    if action == "wake-route":
        old, new = json.loads(before), json.loads(after)
        old["gremlin-town-wake"]["deliver"] = "discord"
        assert old == new
    if action == "mail-pings":
        assert mm.delivery_route(home) == "discord:12345"
    with pytest.raises(installer.InstallError, match="changed since"):
        installer.apply(home, config, action, plan)


@pytest.mark.parametrize("change", ["source", "target"])
def test_stale_plan_refuses_without_backup_or_write(profile, change):
    home, config, plan = profile
    installer.prepare(home, config, "home-channel", plan)
    path = config if change == "source" else home / ".env"
    path.write_bytes(path.read_bytes() + b"\n# concurrent change\n")
    expected = (home / ".env").read_bytes()
    with pytest.raises(installer.InstallError, match="changed since"):
        installer.apply(home, config, "home-channel", plan)
    assert (home / ".env").read_bytes() == expected
    assert not (home / "discord-home-backups").exists()


def test_change_during_backup_refuses_without_overwrite(profile, monkeypatch):
    home, config, plan = profile
    installer.prepare(home, config, "home-channel", plan)
    original = installer.exclusive_file
    def write(path, blob, mode=0o600):
        original(path, blob, mode)
        if path.name == "before":
            (home / ".env").write_bytes(b"# concurrent operator change\n")
    monkeypatch.setattr(installer, "exclusive_file", write)
    with pytest.raises(installer.InstallError, match="during backup"):
        installer.apply(home, config, "home-channel", plan)
    assert (home / ".env").read_bytes() == b"# concurrent operator change\n"


def test_home_change_replaces_only_one_native_setting_and_preserves_crlf():
    source = b'# one\r\nDISCORD_HOME_CHANNEL="999" # old\r\nTELEGRAM_HOME_CHANNEL=keep\r\n'
    settings = {"diablo_home": True, "home_channel_id": "12345"}
    assert installer.build("home-channel", source, settings) == (
        b"# one\r\nDISCORD_HOME_CHANNEL=12345\r\nTELEGRAM_HOME_CHANNEL=keep\r\n")
    with pytest.raises(installer.InstallError, match="Duplicate"):
        installer.build("home-channel", source + b"DISCORD_HOME_CHANNEL=another\n", settings)


def test_disabled_settings_retain_profile_and_switch_wake_back_to_telegram(profile):
    home, config, plan = profile
    settings = installer.gates(b"[discord]\n")
    original = (home / ".env").read_bytes()
    assert installer.build("home-channel", original, settings) == original
    route = json.loads((home / "webhook_subscriptions.json").read_bytes())
    route["gremlin-town-wake"]["deliver"] = "discord"
    after = installer.build("wake-route", json.dumps(route).encode(), settings)
    assert json.loads(after)["gremlin-town-wake"]["deliver"] == "telegram"
    route_file(home)
    (home / "mail-monitor/delivery.json").write_bytes(installer.build("mail-pings", None, settings))
    assert mm.delivery_route(home) is None


@pytest.mark.parametrize("field,value", [("deliver", "telegram,discord"), ("events", ["unexpected"]),
    ("toolsets", ["different"]), ("deliver_extra", {"chat_id": "synthetic"})])
def test_unfamiliar_wake_route_stops(profile, field, value):
    home, config, plan = profile
    path = home / "webhook_subscriptions.json"
    data = json.loads(path.read_text())
    data["gremlin-town-wake"][field] = value
    path.write_text(json.dumps(data))
    with pytest.raises(installer.InstallError, match="unfamiliar"):
        installer.prepare(home, config, "wake-route", plan)
    assert not plan.exists()


def test_cli_failure_never_reflects_input_content_or_os_error(profile, monkeypatch, capsys):
    home, config, plan = profile
    def fail(*args):
        raise OSError("private synthetic credential")
    monkeypatch.setattr(installer, "prepare", fail)
    assert installer.main(["prepare", "home-channel", "--hermes-home", str(home),
                           "--gt-config", str(config), "--plan", str(plan)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and "private synthetic credential" not in captured.err


def test_new_projection_inherits_profile_owner(profile, monkeypatch):
    home, config, plan = profile
    owners = []
    monkeypatch.setattr(installer.os, "chown", lambda path, uid, gid: owners.append((Path(path), uid, gid)),
                        raising=False)
    installer.prepare(home, config, "mail-pings", plan)
    installer.apply(home, config, "mail-pings", plan)
    owner = home.stat()
    assert len(owners) == 2
    assert owners[0] == (home / "mail-monitor", owner.st_uid, owner.st_gid)
    assert owners[1][1:] == (owner.st_uid, owner.st_gid)


def test_cli_bad_arguments_never_echo_values(capsys):
    with pytest.raises(SystemExit) as result:
        installer.main(["private-synthetic-credential"])
    assert result.value.code == 2
    assert "private-synthetic-credential" not in capsys.readouterr().err


def test_native_env_accepts_leading_space_without_creating_duplicate():
    settings = {"diablo_home": True, "home_channel_id": "12345"}
    source = b"  export DISCORD_HOME_CHANNEL = '999'\nOTHER=keep\n"
    assert installer.build("home-channel", source, settings) == b"DISCORD_HOME_CHANNEL=12345\nOTHER=keep\n"


def test_main_routes_confirmed_ping_and_recovery_without_telegram_copy(home, monkeypatch, capsys):
    route_file(home)
    state(home)
    mm.save_state(mm.failure_path(home), {"cause": "synthetic"})
    monkeypatch.setattr(mm, "hermes_home", lambda: home)
    sent = []
    def send(args, **kwargs):
        sent.append(kwargs["input"])
        return SimpleNamespace(returncode=0, stdout='{"success": true}')
    monkeypatch.setattr(mm.subprocess, "run", send)
    original = mm.run
    monkeypatch.setattr(mm, "run", lambda h, **kwargs: original(h, http=FakeGmail(["mail-a"]),
        scorer=FakeScorer(default=8), now=lambda: NOW, **kwargs))
    assert mm.main([]) == 0
    assert len(sent) == 2 and sent[1] == mm.RECOVERED
    assert capsys.readouterr() == ("", "")


def test_main_fallback_hands_ping_to_telegram_once(home, monkeypatch, capsys):
    route_file(home)
    state(home)
    monkeypatch.setattr(mm, "hermes_home", lambda: home)
    sent = []
    def send(args, **kwargs):
        sent.append(kwargs["input"])
        return SimpleNamespace(returncode=1, stdout="{}")
    monkeypatch.setattr(mm.subprocess, "run", send)
    original = mm.run
    monkeypatch.setattr(mm, "run", lambda h, **kwargs: original(h, http=FakeGmail(["mail-a"]),
        scorer=FakeScorer(default=8), now=lambda: NOW, **kwargs))
    assert mm.main([]) == 0
    output = capsys.readouterr().out
    assert len(sent) == 1 and output.count("Important email") == 1
    assert output.startswith(mm.TELEGRAM_FALLBACK)


@pytest.mark.parametrize("source", [
    b'OTHER="first line\nDISCORD_HOME_CHANNEL=not-an-assignment\nlast line"\n',
    b'DISCORD_HOME_CHANNEL="123\n456"\n',
    b"OTHER='first line\nDISCORD_HOME_CHANNEL=not-an-assignment\nlast line'\n",
    b'OTHER="escaped \\" quote\ncontinued"\n',
    b'not an understood assignment\n',
])
def test_multiline_or_unfamiliar_env_refused_without_preparing_change(profile, source):
    home, config, plan = profile
    target = home / ".env"
    target.write_bytes(source)
    with pytest.raises(installer.InstallError, match="syntax needs checking"):
        installer.prepare(home, config, "home-channel", plan)
    assert target.read_bytes() == source and not plan.exists()


def test_single_line_quoted_env_is_preserved_byte_for_byte():
    source = b'''# comment\nOTHER="escaped \\" quote" # retained\nSINGLE='quoted value'\nPLAIN=literal"quote\n'''
    result = installer.build("home-channel", source, {"diablo_home": True, "home_channel_id": "12345"})
    assert result == source + b"DISCORD_HOME_CHANNEL=12345\n"


def test_canonical_gate_change_during_temp_creation_refuses_publication(profile, monkeypatch):
    home, config, plan = profile
    before = (home / ".env").read_bytes()
    installer.prepare(home, config, "home-channel", plan)
    original = installer.os.chmod
    def chmod(path, mode):
        original(path, mode)
        if Path(path).name.startswith(".discord-home-"):
            config.write_text("[discord]\nenabled=false\nmail_pings=false\ndiablo_home=false\n")
    monkeypatch.setattr(installer.os, "chmod", chmod)
    with pytest.raises(installer.InstallError, match="changed before replacement"):
        installer.apply(home, config, "home-channel", plan)
    assert (home / ".env").read_bytes() == before
