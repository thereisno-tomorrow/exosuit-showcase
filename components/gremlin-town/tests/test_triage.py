import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from gremlin_town import triage
from gremlin_town.entrypoint import main as entrypoint_main
from gremlin_town.mcp.brain_usage import BrainUsageEvent, BrainUsageLog
from gremlin_town.storage.migrations import MIGRATIONS

NOW = datetime(2026, 9, 26, 22, 30, tzinfo=UTC)


def test_returned_counts_follow_proof_check_and_fourteen_day_open_rule(hub):
    tmp_path, db, config, _ = hub
    for key in ("unchecked", "proved", "accepted", "unverified", "stale"):
        add_request(db, f"req_{key}", "hermes-wa", "result_returned", NOW - timedelta(days=14) if key == "stale" else NOW)
        proof = {"proved": "https://example.test/result", "unverified": "op_missing"}.get(key)
        for event, detail in (("result_reserved", {"proof": proof} if proof else {}), ("result_returned", {})):
            db.execute("INSERT INTO events(request_id,operation_id,actor_id,event_type,detail_json,occurred_at) VALUES (?,?,?,?,?,?)",
                       (f"req_{key}", f"op_{key}", "hermes-wa", event, json.dumps(detail),
                        (NOW - timedelta(days=14) if key == "stale" else NOW).isoformat()))
    db.execute("INSERT INTO events(request_id,actor_id,event_type,detail_json,occurred_at) VALUES (?,?,?,?,?)",
               ("req_accepted", "codex", "result_check_accepted", "{}", NOW.isoformat()))
    db.commit()
    _, stats = triage.gt_findings(tmp_path / "gt.sqlite3", NOW - timedelta(days=1), NOW)
    assert stats["results_returned"] == 4
    assert stats["open_requests"] == 2


@pytest.fixture
def hub(tmp_path):
    database = tmp_path / "gt.sqlite3"
    db = sqlite3.connect(database)
    for version in sorted(MIGRATIONS):
        db.executescript(MIGRATIONS[version])
    staging = tmp_path / "staging"
    staging.mkdir()
    config = tmp_path / "config.toml"
    config.write_text(
        f'[server]\ndatabase = "{database.as_posix()}"\n'
        f'[brain_core]\nenabled = true\ndecision_staging_dir = "{staging.as_posix()}"\n',
        encoding="utf-8",
    )
    return tmp_path, db, config, staging / "brain-usage.sqlite3"


def add_request(db, request_id, recipient, state, updated):
    stamp = updated.isoformat()
    db.execute(
        "INSERT INTO requests(request_id, mode, project, requester_id, recipient_id, objective, sensitivity,"
        " work_state, revision, created_at, updated_at, backend_binding)"
        " VALUES (?, 'action_requested', 'p', 'codex', ?, 'o', 'internal', ?, 1, ?, ?, 'github')",
        (request_id, recipient, state, stamp, stamp),
    )
    db.commit()


def log_calls(path, when, events):
    log = BrainUsageLog(path, clock=lambda: when)
    for event in events:
        log.record(event)
    log.close()


def test_run_finds_bc_and_gt_trouble_and_writes_report(hub):
    tmp_path, db, config, usage = hub
    add_request(db, "req_stalled", "hermes-wa", "acknowledged", NOW - timedelta(days=2))
    add_request(db, "req_fresh", "fleet-boss-grok", "requested", NOW - timedelta(minutes=10))
    log_calls(usage, NOW - timedelta(hours=3), [
        BrainUsageEvent("local-codex", "http", "memory.save", "ok", None, 31_000, "op-1"),
        BrainUsageEvent("chatgpt-web", "mcp", "memory.search", "error", "brain.invalid_argument", 20),
        BrainUsageEvent("unknown", "http", "memory.read", "error", "UNAUTHORIZED", 5),
    ])
    report = triage.run(config, tmp_path / "triage", now=NOW)
    keys = {f["key"]: f for f in report["findings"]}
    assert set(keys) == {
        "bc.slow:memory.save", "bc.error:memory.search:brain.invalid_argument",
        "bc.unauthenticated:memory.read", "gt.stalled:hermes-wa",
    }
    assert keys["gt.stalled:hermes-wa"]["refs"] == ["req_stalled"]
    assert keys["bc.slow:memory.save"]["title"] == "Slow Brain memory.save (over 15 s)"
    modes = {i["key"]: i["fix_mode"] for i in report["issues"]}
    assert modes["bc.error:memory.search:brain.invalid_argument"] == "auto"
    assert modes["gt.stalled:hermes-wa"] == "approval"
    assert report["brain"] == {"calls": 3, "errors": 2, "by_actor": {"local-codex": 1, "chatgpt-web": 1, "unknown": 1}}
    saved = json.loads((tmp_path / "triage" / "latest.json").read_text(encoding="utf-8"))
    assert saved["schema"] == "gremlin-town.triage/v1"
    assert "Requests to hermes-wa started but no progress" in (tmp_path / "triage" / "latest.txt").read_text(encoding="utf-8")


def test_fix_lifecycle_resolves_after_quiet_week_and_reopens_on_recurrence(hub):
    tmp_path, _, config, usage = hub
    state = tmp_path / "triage"
    bad = BrainUsageEvent("chatgpt-web", "mcp", "memory.search", "error", "brain.invalid_argument", 20)
    key = "bc.error:memory.search:brain.invalid_argument"
    log_calls(usage, NOW - timedelta(hours=1), [bad])
    triage.run(config, state, now=NOW)

    store = triage.IssueStore(state / "triage.sqlite3", clock=lambda: NOW)
    store.mark(key, shipped="commit abc123", note="clearer tool description")
    store.close()

    day = NOW
    for _ in range(triage.RESOLVE_AFTER_DAYS):
        day += timedelta(days=1)
        report = triage.run(config, state, now=day)
    assert {i["key"]: i["status"] for i in report["issues"]}[key] == "resolved"

    log_calls(usage, day + timedelta(hours=1), [bad])
    report = triage.run(config, state, now=day + timedelta(days=1))
    issue = {i["key"]: i for i in report["issues"]}[key]
    assert (issue["status"], issue["reopened"]) == ("open", 1)
    assert any("Fix did not hold" in alert for alert in report["alerts"])


def test_operator_issue_clears_when_live_state_clears(hub):
    tmp_path, db, config, _ = hub
    add_request(db, "req_stalled", "hermes-wa", "acknowledged", NOW - timedelta(days=2))
    triage.run(config, tmp_path / "triage", now=NOW)
    db.execute("UPDATE requests SET work_state='result_returned' WHERE request_id='req_stalled'")
    db.commit()
    report = triage.run(config, tmp_path / "triage", now=NOW + timedelta(days=1))
    assert {i["key"]: i["status"] for i in report["issues"]}["gt.stalled:hermes-wa"] == "resolved"


def test_uncertain_send_raises_alert(hub):
    tmp_path, db, config, _ = hub
    add_request(db, "req_1", "hermes-wa", "result_returned", NOW)
    db.execute(
        "INSERT INTO operations(operation_id, client_operation_id, actor_id, request_id, operation_kind,"
        " input_digest, accepted_at, operation_state) VALUES ('op_1','c1','codex','req_1','reply_result','d',?, 'uncertain')",
        (NOW.isoformat(),),
    )
    db.commit()
    report = triage.run(config, tmp_path / "triage", now=NOW)
    assert "GT sends in state 'uncertain': 1" in report["alerts"]


def test_weekly_summary_and_cli(hub, capsys):
    tmp_path, _, config, usage = hub
    state = tmp_path / "triage"
    log_calls(usage, NOW - timedelta(hours=1), [
        BrainUsageEvent("local-codex", "http", "memory.search", "ok", None, 900),
        BrainUsageEvent("hermes-wa", "http", "memory.save", "ok", None, 2_000, "op-2"),
    ])
    triage.run(config, state, now=NOW)
    text = triage.weekly(state, now=NOW)
    assert "Brain Core: 2 calls, error rate 0%" in text
    assert "local-codex 1" in text and "hermes-wa 1" in text

    assert entrypoint_main(["triage", "show", "--state-dir", str(state)]) == 0
    assert "GT + Brain Core triage" in capsys.readouterr().out
    assert entrypoint_main(["triage", "mark", "--state-dir", str(state), "--key", "missing", "--dismiss"]) == 2
    assert entrypoint_main(["triage", "weekly", "--state-dir", str(state)]) == 0
    assert "Weekly GT + Brain Core summary" in capsys.readouterr().out
