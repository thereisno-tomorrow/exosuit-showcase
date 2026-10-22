"""Upgrade preparation safety and failure gates; no hub, credential or model calls."""
from __future__ import annotations

import importlib.util
import json
import hashlib
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "adapters/hermes/upgrade"


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


check = module("upgrade_check", HERE / "upgrade_check.py")
rollback = module("upgrade_rollback", HERE / "rollback.py")
fresh = module("upgrade_reset", HERE / "fresh_start.py")
restart = module("upgrade_restart", HERE / "restart_guard.py")
scorer = module("documented_mail_scorer", ROOT / "adapters/hermes/scripts/mail_scorer.py")


@pytest.mark.parametrize("name", [".env", "auth.json", "google_token.json", "secrets/token", "../token", "/etc/passwd",
                                  "plugins/brain/token.json", "skills/personal/SKILL.md"])
def test_readcheck_refuses_credential_and_unknown_paths(name):
    assert not check.allowed_pin(name)


def fixture_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text("{}", encoding="utf-8")
    data = {"pins": {}, "config_sha256": check.digest(config)}
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps(data), encoding="utf-8")
    return home, baseline


def test_pending_never_returns_safe_and_target_home_unchanged(tmp_path, monkeypatch):
    home, baseline = fixture_home(tmp_path)
    before = {p.name: p.read_bytes() for p in home.iterdir()}
    def runner(argv, **kwargs):
        assert kwargs["env"]["HERMES_HOME"] != str(home)
        assert "OPENAI_API_KEY" not in kwargs["env"]
        assert "GREMLIN_TOWN_HUB_TOKEN" not in kwargs["env"]
        return SimpleNamespace(returncode=0, stdout="[]", stderr="")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-never-forwarded")
    monkeypatch.setattr(check.subprocess, "run", runner)
    report = check.check(home, Path(sys.executable), tmp_path, baseline)
    assert report["status"] == "needs the hub" and report["safe_to_switch"] is False
    assert before == {p.name: p.read_bytes() for p in home.iterdir()}


def test_changed_config_and_failed_probe_are_red_and_private_output_suppressed(tmp_path, monkeypatch):
    home, baseline = fixture_home(tmp_path)
    (home / "config.yaml").write_text("changed", encoding="utf-8")
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **kw: SimpleNamespace(
        returncode=1, stdout="PRIVATE", stderr="PRIVATE"))
    report = check.check(home, Path(sys.executable), tmp_path, baseline)
    assert report["status"] == "fail" and not report["safe_to_switch"]
    assert "PRIVATE" not in json.dumps(report)


def test_baseline_path_escape_refused_before_probe(tmp_path, monkeypatch):
    home, baseline = fixture_home(tmp_path)
    baseline.write_text(json.dumps({"pins": {"../auth.json": "x"}}), encoding="utf-8")
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **kw: pytest.fail("probe must not launch"))
    with pytest.raises(ValueError):
        check.check(home, Path(sys.executable), tmp_path, baseline)


def test_config_link_refused_before_read_or_probe(tmp_path, monkeypatch):
    home, baseline = fixture_home(tmp_path)
    monkeypatch.setattr(Path, "is_symlink", lambda path: path.name == "config.yaml")
    monkeypatch.setattr(check, "digest", lambda path: pytest.fail("linked config must not be read"))
    monkeypatch.setattr(check.subprocess, "run", lambda *a, **kw: pytest.fail("probe must not launch"))
    with pytest.raises(ValueError):
        check.check(home, Path(sys.executable), tmp_path, baseline)


@pytest.fixture
def scorer_home(tmp_path):
    home = tmp_path / "scorer"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps(scorer.CONFIG), encoding="utf-8")
    return home


def test_documented_scorer_no_tools_no_inherited_credentials_and_exact_ids(scorer_home, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fixture-private")
    def run(argv, **kw):
        assert argv[:2] == ["hermes", "-z"] and "--ignore-rules" in argv
        assert argv[argv.index("--model") + 1] == "gpt-6-luna"
        assert "TELEGRAM_BOT_TOKEN" not in kw["env"]
        assert kw["cwd"] == scorer_home
        return SimpleNamespace(returncode=0, stdout='[{"index":0,"score":8,"reason":"today"}]')
    assert scorer.score([{"id": "only-input-id"}], home=scorer_home, criteria="rule", runner=run) == [
        {"id": "only-input-id", "score": 8, "reason": "today"}]


@pytest.mark.parametrize("output", ['[]', '{}', 'private-token', '[{"index":0,"score":true,"reason":"x"}]',
    '[{"index":1,"score":8,"reason":"x"}]', '[{"index":0,"score":11,"reason":"x"}]'])
def test_bad_model_output_fails_without_echoing(scorer_home, output):
    with pytest.raises(scorer.ScorerError) as error:
        scorer.score([{"id": "x"}], home=scorer_home, criteria="rule",
                     runner=lambda *a, **kw: SimpleNamespace(returncode=0, stdout=output))
    assert output not in str(error.value)


def test_scorer_subprocess_failure_suppresses_private_stderr(scorer_home):
    with pytest.raises(scorer.ScorerError, match="exit 4") as error:
        scorer.score([{"id": "x"}], home=scorer_home, criteria="rule", runner=lambda *a, **kw:
                     SimpleNamespace(returncode=4, stdout="private", stderr="private"))
    assert "private" not in str(error.value).replace("private output suppressed", "")


def test_scorer_refuses_custom_plugins_or_wide_config(scorer_home):
    (scorer_home / "plugins").mkdir()
    (scorer_home / "plugins/plugin.py").write_text("unsafe")
    with pytest.raises(scorer.ScorerError):
        scorer.validate_home(scorer_home)


def test_scorer_allows_only_pinned_seeded_soul_and_refuses_custom_context(scorer_home, monkeypatch):
    default = "Public auto-seeded fixture default"
    monkeypatch.setattr(scorer, "AUTO_SEEDED_SOUL_SHA256", hashlib.sha256(default.encode()).hexdigest())
    (scorer_home / "SOUL.md").write_text(default + "\n", encoding="utf-8")
    scorer.validate_home(scorer_home)
    (scorer_home / "SOUL.md").write_text(default + " private customization", encoding="utf-8")
    with pytest.raises(scorer.ScorerError, match="custom context"):
        scorer.validate_home(scorer_home)


def test_documented_monitor_check_never_calls_private_scorer_or_model(scorer_home, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "adapters/hermes/scripts"))
    candidate = module("candidate_documented_monitor", ROOT / "adapters/hermes/scripts/mail_monitor_documented.py")
    monkeypatch.setenv("MAIL_SCORER_HOME", str(scorer_home))
    monkeypatch.setattr(candidate.monitor, "load_login", lambda path: {"fixture": True})
    monkeypatch.setattr(candidate.monitor, "access_token", lambda login, http: "noncredential-fixture")
    monkeypatch.setattr(candidate.mail_scorer, "score", lambda *a, **kw: pytest.fail("check must not score"))
    before = list(tmp_path.rglob("*"))
    text = candidate.check(tmp_path, http=lambda *a: {"messages": [{"id": "fixture"}]})
    assert "Nothing saved or sent" in text
    assert list(tmp_path.rglob("*")) == before


def test_documented_monitor_main_restores_shared_functions_after_check(scorer_home, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "adapters/hermes/scripts"))
    candidate = module("candidate_monitor_entrypoint", ROOT / "adapters/hermes/scripts/mail_monitor_documented.py")
    original = candidate.monitor.run
    old_check = candidate.monitor.check
    monkeypatch.setattr(candidate, "check", lambda home: "fixture check")
    for _ in range(2):
        assert candidate.main(["--check"]) == 0
        assert candidate.monitor.run is original and candidate.monitor.check is old_check


def test_reset_boundary_and_idempotent_request(tmp_path):
    before = datetime(2026, 10, 3, 19, 59, tzinfo=timezone.utc)
    after = before + timedelta(minutes=1)
    assert fresh.reset_day(before) == "2026-10-03"
    assert fresh.reset_day(after) == "2026-10-04"
    assert fresh.request(tmp_path, after).startswith("requested")
    assert fresh.request(tmp_path, after).startswith("already requested")
    assert not (tmp_path / "sessions").exists()
    assert json.loads((HERE / "reset-options.json").read_text())["selected"] == "none (manual /new)"


def test_rollback_rejects_live_or_unmarked_target(tmp_path):
    with pytest.raises(ValueError):
        rollback.restore(tmp_path, "previous")


@pytest.fixture
def confined_practice_path():
    """Own one disposable practice directory inside the rollback's allowed root."""
    repository = ROOT.parents[1].resolve()
    staging = repository / ".tmp"
    staging.mkdir(exist_ok=True)
    assert staging.resolve().parent == repository, "Practice staging escaped the repository"
    with TemporaryDirectory(prefix="rollback-test-", dir=staging) as directory:
        practice = Path(directory).resolve()
        assert practice.parent == staging.resolve()
        yield practice


def test_rollback_restores_exact_home_and_version_preserving_displaced_data(confined_practice_path):
    tmp_path = confined_practice_path
    (tmp_path / "practice-only.json").write_text(json.dumps({"practice_only": True, "gateways": False, "credentials": False}))
    (tmp_path / "home").mkdir()
    (tmp_path / "home/config.yaml").write_bytes(b"original")
    (tmp_path / "active-version.json").write_bytes(b'{"version":"0.20.5"}')
    saved = rollback.snapshot(tmp_path, "previous")
    (tmp_path / "home/config.yaml").write_bytes(b"candidate")
    (tmp_path / "home/extra.txt").write_bytes(b"retain this")
    (tmp_path / "active-version.json").write_bytes(b'{"version":"0.21.5"}')
    displaced = rollback.restore(tmp_path, "previous")
    assert (tmp_path / "home/config.yaml").read_bytes() == b"original"
    assert not (tmp_path / "home/extra.txt").exists()
    assert (displaced / "extra.txt").read_bytes() == b"retain this"
    assert json.loads((tmp_path / "active-version.json").read_bytes())["version"] == "0.20.5"
    (saved / "home/config.yaml").write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="integrity"):
        rollback.restore(tmp_path, "previous")
    assert (tmp_path / "home/config.yaml").read_bytes() == b"original"


def test_connection_markers_require_new_start_and_all_platforms():
    start = datetime(2026, 10, 3, 10, 0, tzinfo=timezone.utc)
    lines = [{"timestamp": (start - timedelta(seconds=1)).isoformat(), "message": "Connected to Telegram"},
             {"timestamp": start.isoformat(), "message": "Connected to Discord"}]
    assert restart.connections(lines, start, {"telegram": "Connected to Telegram", "discord": "Connected to Discord"}) == {
        "telegram": False, "discord": True}


def test_restart_requires_fresh_idle_matching_container():
    now = datetime.now(timezone.utc)
    observation = {"container": "hermes-wa", "mount": "/srv/hermes-example", "observed_at": now.isoformat(),
                   "active_turns": 0, "active_jobs": 0, "ready": True}
    assert restart.eligible(observation, now)
    for changed in ({"active_turns": 1}, {"active_jobs": True}, {"container": "ciel"},
                    {"observed_at": (now - timedelta(seconds=61)).isoformat()}):
        assert not restart.eligible({**observation, **changed}, now)


def test_reliance_covers_each_plan_piece_and_machine_hub_gates():
    reliance = json.loads((HERE.parent / "HERMES-RELIANCE.json").read_text(encoding="utf-8"))
    assert len(reliance["pieces"]) >= 13
    assert all(p["checks"] and p["sources"] and p["hermes_contract"] for p in reliance["pieces"])
    assert {"hub:manual-new", "hub:full-tool-budget", "hub:conversation", "hub:connections"} <= reliance["hub_checks"].keys()
