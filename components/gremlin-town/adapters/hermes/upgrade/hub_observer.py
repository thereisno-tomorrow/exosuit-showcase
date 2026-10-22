"""Read-only practice evidence, bound to the independently inspected start.

No credential files, transcripts, message content or raw Docker logs are emitted.
Missing evidence stays pending. This never launches, restarts, repairs or switches.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def command(argv):
    done = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if done.returncode:
        raise RuntimeError("observer command failed; private diagnostics suppressed")
    return done.stdout.strip()


def discord_connected(home: Path, start: str, now: datetime | None = None) -> bool:
    """Native heartbeat, with stale and previous-container records refused."""
    path = home / "gateway_state.json"
    if not path.is_file() or path.is_symlink():
        return False
    record = json.loads(path.read_text())
    platform = record.get("platforms", {}).get("discord", {})
    if platform.get("state") != "connected":
        return False
    try:
        began = datetime.fromisoformat(start.replace("Z", "+00:00"))
        updated = datetime.fromisoformat(record["updated_at"].replace("Z", "+00:00"))
        ready = datetime.fromisoformat(platform["updated_at"].replace("Z", "+00:00"))
        age = ((now or datetime.now(UTC)) - updated).total_seconds()
        return ready >= began and updated >= ready and 0 <= age <= 120
    except (KeyError, TypeError, ValueError):
        return False


def native_receipts(home: Path, start: str) -> dict:
    """Bounded synthetic tool receipts and reset metadata, never message output."""
    path = home / "state.db"
    result = {"manual_new_count": 0, "cron_help_then_list": False}
    if not path.is_file() or path.is_symlink():
        return result
    since = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        result["manual_new_count"] = db.execute(
            "SELECT COUNT(*) FROM sessions WHERE source=? AND ended_at>=? AND end_reason=?",
            ("discord", since, "session_reset")).fetchone()[0]
        rows = db.execute(
            "SELECT m.tool_name,m.content FROM messages m JOIN sessions s ON s.id=m.session_id "
            "WHERE s.source=? AND m.role=? AND m.timestamp>=? AND m.tool_name IN (?,?) ORDER BY m.timestamp",
            ("discord", "tool", since, "gremlin_town_more_tools", "cronjob_manage"))
        helped = False
        for name, content in rows:
            value = json.loads(content)
            body = value.get("result")
            if name == "gremlin_town_more_tools" and value.get("ok") is True and isinstance(body, dict) and body.get("name") == "cronjob_manage":
                helped = True
            if name == "cronjob_manage" and helped and value.get("success") is True and isinstance(value.get("jobs"), list):
                result["cron_help_then_list"] = True
    return result


def ancillary_receipts(root: Path, container: str, start: str) -> dict:
    """Private fixture receipts plus independent native scheduler/store reads.

    These are partial proofs: synthetic hook metadata and a no-agent tick never
    stand in for the full sender, monitor, compression or soak gates.
    """
    result = {"courier": False, "natural_no_agent": False,
              "words_registered_hook": False, "outbox_replayed_once": False, "natural_idle": False}
    def receipt(path):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 20_000:
            return None
        value = json.loads(path.read_text())
        return value if (value.get("fixture_only") is True and value.get("container") == container
                         and value.get("started_at") == start) else None
    courier = receipt(root / "evidence/courier-fixture.json")
    if courier:
        rows = courier.get("deliveries", [])
        result["courier"] = (len(rows) == 2 and {r.get("kind") for r in rows} == {"review", "mail"}
                             and all(r.get("remote_observed") is True and r.get("message_id") for r in rows))
    words = receipt(root / "evidence/words-hook-fixture.json")
    if words:
        fields = ("exact", "changed_detected", "explicit_quote", "other_sender", "parent_session", "plaintext_absent")
        result["words_registered_hook"] = all(words.get("native_registered_hook_and_handler", {}).get(k) is True for k in fields)
    home = root / "home"
    job = receipt(root / "evidence/natural-job-created.json")
    ledger = home / "cron/executions.db"
    ticks = home / "practice-natural-ticks.jsonl"
    final_ticks = home / "practice-final-ticks.jsonl"
    if final_ticks.is_file() and not ticks.exists():
        ticks = final_ticks
    if job and job.get("manual_run_invoked") is False and ledger.is_file() and not ledger.is_symlink() and ticks.is_file() and not ticks.is_symlink():
        with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            runs = db.execute("SELECT source,status,delivery_outcome,scheduled_instant FROM executions WHERE job_id=?", (job["job_id"],)).fetchall()
        rows = [json.loads(line) for line in ticks.read_text().splitlines()]
        due = runs[0][3] if len(runs) == 1 else None
        result["natural_no_agent"] = (len(runs) == 1 and runs[0][:3] == ("builtin", "completed", "suppressed") and len(rows) == 1
            and rows[0].get("fixture_only") is True
            and isinstance(due, str) and datetime.fromisoformat(due) >= datetime.fromisoformat(start.replace("Z", "+00:00"))
            and datetime.fromisoformat(rows[0]["at"]) >= datetime.fromisoformat(due))
    replay = receipt(home / "practice-memory-replay.json")
    fixture = root / "fixture/fixture.sqlite3"
    if replay and fixture.is_file() and not fixture.is_symlink():
        with sqlite3.connect(fixture.as_uri() + "?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            count = db.execute("SELECT COUNT(*) FROM continuity_markers WHERE id=?", (replay["replayed_operation_id"],)).fetchone()[0]
            # Retained earlier rehearsals are not this run's natural idle effect.
            idle_rows = db.execute("SELECT body FROM continuity_markers WHERE id LIKE 'hermes-idle-%'").fetchall()
            began = datetime.fromisoformat(replay["at"]).replace(second=0, microsecond=0)
            idle_count = 0
            for body, in idle_rows:
                value = json.loads(body).get("value", "")
                match = re.match(r"idle: no turn since (\d{4}-\d\d-\d\dT\d\d:\d\dZ);", value)
                if match and datetime.fromisoformat(match[1].replace("Z", "+00:00")) >= began:
                    idle_count += 1
        result["outbox_replayed_once"] = count == 1
        idle = receipt(home / "practice-memory-watch-idle.json")
        result["natural_idle"] = bool(idle and idle.get("idle_marked") is True and idle_count == 1
                                     and idle.get("natural_idle_elapsed_seconds", 0) >= 1800)
    return result


def completed_acceptance(root: Path, container: str, start: str, command_fn=command) -> dict:
    """Read completed fixture gates; missing or prior-start evidence never passes."""
    result = dict.fromkeys(("brain_flow", "mail", "scheduler", "compression", "rollback"), False)
    home = root / "home"
    def receipt(path):
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 20000:
            return None
        value = json.loads(path.read_text())
        return value if value.get("fixture_only") is True and value.get("container") == container and value.get("started_at") == start else None
    brain = receipt(root / "evidence/brain-flow-fixture.json")
    fixture = root / "fixture/fixture.sqlite3"
    if brain and fixture.is_file() and not fixture.is_symlink():
        with sqlite3.connect(fixture.as_uri() + "?mode=ro", uri=True) as db:
            saved = db.execute("SELECT receipt FROM operations WHERE id=?", ("practice-amber-lantern-20261005",)).fetchone()
            answers = [json.loads(row[0]) for row in db.execute("SELECT body FROM review_answers")]
            recalled = db.execute("SELECT COUNT(*) FROM calls WHERE operation='memory.search' AND at>=?", (start,)).fetchone()[0] > 0
            record_id = json.loads(saved[0]).get("record_id") if saved else None
            result["brain_flow"] = bool(record_id and recalled and sum(a.get("response") == "picked_today" and record_id in a.get("item_refs", []) for a in answers) == 1
                and all(brain.get(k) is True for k in ("actual_native_cli_conversation", "independent_save_receipt", "recall", "review_answer")))
    mail = receipt(root / "evidence/mail-flow-fixture.json")
    result["mail"] = bool(mail and mail.get("real_mailbox_access") is False and all(mail.get(k) is True for k in (
        "synthetic_mail_input", "actual_documented_native_scorer", "independent_discord_delivery", "one_score_one_delivery", "second_run_suppressed", "cursor_readback")))
    job = receipt(root / "evidence/monitor-job-created.json")
    ledger = home / "cron/executions.db"
    if job and job.get("manual_run_invoked") is False and ledger.is_file() and not ledger.is_symlink():
        with sqlite3.connect(ledger.as_uri() + "?mode=ro", uri=True) as db:
            runs = db.execute("SELECT source,status,delivery_outcome,scheduled_instant FROM executions WHERE job_id=?", (job["job_id"],)).fetchall()
        model = [json.loads(line) for line in (home / "practice-observer.jsonl").read_text().splitlines()]
        model = [r for r in model if r.get("container_started_at") == start and r.get("event") == "llm_execution" and r.get("platform") == "cron"]
        began = datetime.fromisoformat(start.replace("Z", "+00:00"))
        result["scheduler"] = len(runs) == 2 and all(row[:3] == ("builtin", "completed", "suppressed") and isinstance(row[3], str) and datetime.fromisoformat(row[3]) >= began for row in runs) and len(model) == 1 and model[0].get("within_budget") is True
    compressed = receipt(home / "practice-native-compression.json")
    state = home / "state.db"
    if compressed and state.is_file() and not state.is_symlink() and fixture.is_file() and not fixture.is_symlink():
        with sqlite3.connect(state.as_uri() + "?mode=ro", uri=True) as db:
            sid = compressed.get("session_id")
            active = db.execute("SELECT COUNT(*),SUM(_compressed_summary) FROM messages WHERE session_id=? AND active=1", (sid,)).fetchone()
            compacted = db.execute("SELECT COUNT(*) FROM messages WHERE session_id=? AND compacted=1 AND active=0", (sid,)).fetchone()[0]
        with sqlite3.connect(fixture.as_uri() + "?mode=ro", uri=True) as db:
            marker = db.execute("SELECT body FROM continuity_markers WHERE id=?", (compressed.get("fixture_marker_id"),)).fetchone()
        result["compression"] = bool(active[0] == compressed.get("native_active_messages") and active[1] == 1 and compacted >= 10 and marker
            and json.loads(marker[0]).get("session_id") == "practice-provider-note" and all(compressed.get(k) is True for k in (
                "documented_native_compress_command", "native_in_place_commit", "private_synthetic_ui_success", "independent_fixture_marker", "continuity_note_preserved")))
    rollback_path = root / "evidence/rollback-fixture.json"
    if rollback_path.is_file() and not rollback_path.is_symlink():
        rollback = json.loads(rollback_path.read_text())
        pairs = (("hermes-upgrade-rollback-old-20261004", "previous_image", "rollback_start"),
                 ("hermes-upgrade-rollback-new-20261004", "candidate_image", "forward_start"))
        consistent = True
        for name, image_key, start_key in pairs:
            meta = json.loads(command_fn(["docker", "inspect", "--format", "{{json .}}", name]))
            expected_id = "sha256:" + rollback.get(image_key, "").split("sha256:")[-1]
            consistent = consistent and meta["Image"] == expected_id and meta["State"]["StartedAt"] == rollback.get(start_key) and meta["State"]["Running"] is False
        result["rollback"] = bool(consistent and rollback.get("fixture_only") is True and all(rollback.get(k) is True for k in (
            "exact_stopped_home_restored", "fresh_discord_connection_after_rollback", "retained_all_containers_and_homes"))
            and (root / "rollback-lab/stopped-old-home-pins.json").is_file())
    return result



def observe(root: Path, container: str, image: str):
    if root.is_symlink() or not re.fullmatch(r"hermes-upgrade-practice-[0-9]{8}", container):
        raise ValueError("practice-only root and container required")
    root = root.resolve()
    if root == Path("/") or not root.name.startswith("practice-"):
        raise ValueError("unsafe practice root")
    marker = root / "practice-only.json"
    if marker.is_symlink() or json.loads(marker.read_text()).get("practice_only") is not True:
        raise ValueError("explicit practice marker required")
    home = root / "home"
    identity = command(["docker", "inspect", "--format", "{{.Id}}", container])
    state = json.loads(command(["docker", "inspect", "--format", "{{json .State}}", container]))
    actual_image = command(["docker", "inspect", "--format", "{{.Image}}", container])
    mounts = json.loads(command(["docker", "inspect", "--format", "{{json .Mounts}}", container]))
    if not mounts or any(Path(m["Source"]).resolve() != home for m in mounts):
        raise ValueError("container must mount only its isolated home")
    start = state["StartedAt"]
    checks = []

    def add(name, status, detail):
        checks.append({"check": name, "status": status, "detail": detail})

    add("practice-container", "pass" if state["Running"] and actual_image == image else "fail", "running and pinned image")
    binding = json.loads((home / "practice-starts.json").read_text()).get(container)
    add("start-binding", "pass" if binding == start else "fail", "independent current start matches evidence binding")
    restart_count = int(command(["docker", "inspect", "--format", "{{.RestartCount}}", container]))
    add("unexpected-restarts", "pass" if restart_count == 0 and not state["OOMKilled"] else "fail", "no automatic restart or OOM")
    try:
        cfg = json.loads((home / "config.yaml").read_text())
    except json.JSONDecodeError:
        import yaml  # Supplied by native hub runtime; JSON fixtures need no dependency.
        cfg = yaml.safe_load((home / "config.yaml").read_text())
    webhook = cfg.get("platforms", {}).get("webhook", {})
    extra = webhook.get("extra", {})
    routes = extra.get("routes", {})
    route = routes.get("gremlin-town-wake", {})
    private_wake = (webhook.get("enabled") is True and set(routes) == {"gremlin-town-wake"}
                    and extra.get("port") == 8644 and route.get("secret") == "${PRACTICE_WAKE_SECRET}"
                    and route.get("events") == ["gt.request.ready"] and route.get("deliver") == "discord"
                    and route.get("deliver_extra") == {"chat_id": "123456789012345678"})
    add("gateway-isolation", "pass" if cfg.get("platforms", {}).get("discord", {}).get("enabled") is True
        and all(cfg.get("platforms", {}).get(n, {}).get("enabled") is False for n in ("telegram", "feishu", "api_server"))
        and (webhook.get("enabled") is False or private_wake) else "fail", "private Discord and controlled signed fixture wake only; no published ports checked separately")
    for n in ("telegram", "feishu", "api_server"):
        add("connection:" + n, "inactive", "disabled in practice, not a passing connection test")
    add("manual-reset-policy", "pass" if cfg.get("session_reset", {}).get("mode") == "none" else "fail", "no timed reset")
    connected = discord_connected(home, start)
    add("connection:discord", "pass" if connected else "pending", "current-start native Discord connected state and fresh heartbeat")
    requests = []
    p = home / "practice-observer.jsonl"
    if p.is_file() and not p.is_symlink():
        rows = [json.loads(line) for line in p.read_text().splitlines()]
        rows = [r for r in rows if r.get("container_name") == container and r.get("container_started_at") == start]
        requests = [r for r in rows if r.get("event") == "llm_execution" and r.get("platform") == "discord"]
    else:
        rows = []
    add("hub:full-tool-budget", ("pass" if all(r["within_budget"] for r in requests) else "fail") if requests else "pending",
        "actual current-start Discord requests; total <=63000, each <=12000")
    required_tools = {"bc_memory_save", "bc_memory_list", "bc_memory_change", "bc_memory_search", "bc_work", "gremlin_town_more_tools"}
    add("brain-direct-surface", ("pass" if all(required_tools <= set(r.get("tool_names", [])) for r in requests) else "fail")
        if requests else "pending", "all five Brain tools and more-tools present on every actual request")
    add("schema-stability", ("pass" if len({r["schemas_sha256"] for r in requests}) == 1 else "fail") if requests else "pending",
        "byte-identical tool schemas across observed requests")
    weekly = [r for r in rows if r.get("front_target") == "bc_weekly" and r.get("front_action") == "call"]
    front = [r for r in rows if r.get("tool_name") == "gremlin_town_more_tools"]
    actions = [(r.get("front_action"), r.get("front_target")) for r in front]
    ordered = False
    if len(weekly) == 1:
        try:
            listed = next(i for i, a in enumerate(actions) if a[0] == "list")
            described = next(i for i, a in enumerate(actions) if a == ("describe", "bc_weekly"))
            called = next(i for i, a in enumerate(actions) if a == ("call", "bc_weekly"))
            ordered = listed < described < called
        except StopIteration:
            pass
    add("hub:rare-conversation", "pass" if ordered else "pending" if not weekly else "fail", "one current-start Discord list/describe/call sequence; independently reconcile fixture call below")
    # Only counts from the synthetic fixture database. No record bodies returned.
    fixture = root / "fixture/fixture.sqlite3"
    fixture_counts = {}
    if fixture.is_file() and not fixture.is_symlink():
        with sqlite3.connect(fixture.as_uri() + "?mode=ro", uri=True) as db:
            db.execute("PRAGMA query_only=ON")
            fixture_counts = {"records": db.execute("SELECT COUNT(*) FROM records").fetchone()[0],
                              "review_answers": db.execute("SELECT COUNT(*) FROM review_answers").fetchone()[0],
                              "calls": dict(db.execute("SELECT operation,COUNT(*) FROM calls GROUP BY operation"))}
            current = {}
            since = datetime.fromisoformat(start.replace("Z", "+00:00"))
            for at, operation in db.execute("SELECT at,operation FROM calls"):
                if datetime.fromisoformat(at) >= since:
                    current[operation] = current.get(operation, 0) + 1
            fixture_counts["current_start_calls"] = current
    if weekly and fixture_counts.get("current_start_calls", {}).get("weekly") != 1:
        checks[-1]["status"] = "fail"
    receipts = native_receipts(home, start)
    ancillary = ancillary_receipts(root, container, start)
    add("hub:manual-new", "pass" if receipts["manual_new_count"] > 0 else "pending", "current-start native Discord session-reset metadata; policy has no timed reset")
    add("hub:native-docs", "pass" if receipts["cron_help_then_list"] else "pending", "original native instructions successfully returned before successful native job listing")
    add("hub:courier", "pass" if ancillary["courier"] else "pending", "two synthetic owner-only Discord deliveries independently read back; Telegram inactive in practice")
    for key, detail in (("natural_no_agent", "one natural native tick, completed, silent, independently read from ledger and fixture marker"),
                        ("words_registered_hook", "installed native hook/handler synthetic metadata; actual gateway sender still required"),
                        ("outbox_replayed_once", "actual fixture outage queue and independently observed one marker after replay"),
                        ("natural_idle", "unchanged thirty-minute installed provider watcher and one independent fixture marker")):
        add("practice:" + key, "pass" if ancillary[key] else "pending", detail)
    # A wake passes only with separately verified native completion and remote readback.
    wake_complete = False
    wake_path = root / "evidence/wake-fixture-completed.json"
    wire_path = home / "practice-signed-wake.json"
    if private_wake and wake_path.is_file() and wire_path.is_file() and not wake_path.is_symlink() and not wire_path.is_symlink():
        done = json.loads(wake_path.read_text())
        wire = json.loads(wire_path.read_text())
        wake_complete = all(r.get("fixture_only") is True and r.get("container") == container and r.get("started_at") == start for r in (done, wire)) and all(done.get(k) is True for k in (
            "one_native_completed_session", "native_skill_view_success", "exact_native_reply", "independent_discord_reply", "duplicate_generated_no_second_session")) and all(wire.get(k) is True for k in (
            "bad_signature_rejected", "stale_signature_rejected", "native_accepted", "duplicate_suppressed"))
    add("hub:webhook", "pass" if wake_complete else "pending", "signed native wake, stale/invalid rejection, duplicate suppression, installed skill and independently read Discord reply")
    words_complete = False
    words_path = root / "evidence/final-discord-acceptance.json"
    if words_path.is_file() and not words_path.is_symlink():
        words = json.loads(words_path.read_text())
        words_complete = (words.get("fixture_only") is True and words.get("container") == container
                          and words.get("started_at") == start and words.get("actual_owner_exact_copy") is True)
    if words_complete:
        next(c for c in checks if c["check"] == "practice:words_registered_hook")["status"] = "inactive"
    add("hub:words-shadow", "pass" if words_complete else "pending", "real current-start owner message matched native completed bc_work arguments; no canonical fixture write claimed")
    completed = completed_acceptance(root, container, start)
    for key, label in (("brain_flow", "hub:brain-flow"), ("mail", "hub:mail-score"), ("scheduler", "hub:scheduler"), ("rollback", "hub:rollback")):
        add(label, "pass" if completed[key] else "pending", "separate native fixture acceptance independently reconciled")
    brain_complete = completed["brain_flow"] and completed["compression"] and ancillary["outbox_replayed_once"] and ancillary["natural_idle"]
    add("hub:brain-compression-idle-outbox", "pass" if brain_complete else "pending", "native committed compression, fixture note, natural idle and exactly-once replay")
    add("day-soak", "inactive", "elapsed time alone is not acceptance; require specific passing checks")
    age_hours = (datetime.now(UTC) - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds() / 3600
    if command(["docker", "inspect", "--format", "{{.Id}}", container]) != identity or command(
            ["docker", "inspect", "--format", "{{.State.StartedAt}}", container]) != start:
        add("stable-observation", "fail", "container changed during read")
    else:
        add("stable-observation", "pass", "container and start unchanged during read")
    return {"observed_at": datetime.now(UTC).isoformat(), "container": container, "started_at": start,
            "config_sha256": hashlib.sha256((home / "config.yaml").read_bytes()).hexdigest(),
            "checks": checks, "fixture_counts": fixture_counts, "native_receipts": receipts,
            "ancillary_receipts": ancillary, "observed_discord_requests": len(requests),
            "total_chars": max((r["total_chars"] for r in requests), default=0),
            "largest_chars": max((r["largest_chars"] for r in requests), default=0),
            "continuous_age_hours": round(age_hours, 3), "completed_acceptance": completed,
            "practice_passed": all(c["status"] in ("pass", "inactive") for c in checks if c["check"] != "practice:words_registered_hook"),
            "safe_to_switch": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--image", required=True)
    args = parser.parse_args()
    report = observe(args.root, args.container, args.image)
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["practice_passed"] else 1 if any(c["status"] == "fail" for c in report["checks"]) else 2)
