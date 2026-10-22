"""Daily GT + Brain Core triage: turn logged activity into a tracked issue list.

Reads the GT hub database and the Brain Core usage log (both read-only), groups
trouble into issue keys, and keeps each issue's lifecycle in its own SQLite file:
``open`` -> ``fix_shipped`` (someone marked a fix) -> ``resolved`` (no recurrence for
RESOLVE_AFTER_DAYS) and back to ``open`` if it recurs. Output is a JSON report plus
a plain-text summary for the fix loop and for Nic. Deterministic; no model calls.
"""

from __future__ import annotations

import json
import sqlite3
import tomllib
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from gremlin_town.service.service import REQUEST_STILL_OPEN_SQL, REQUEST_LAST_ACTIVITY_SQL

RESOLVE_AFTER_DAYS = 7
SLOW_WRITE_MS = 15_000
SLOW_READ_MS = 10_000
STALE_PICKUP = timedelta(hours=2)
STALE_WORK = timedelta(hours=24)
STUCK_SEND = timedelta(minutes=30)
ERROR_ALERT_THRESHOLD = 5

BRAIN_WRITES = {"memory.save", "memory.change", "decision.record", "continuity.start", "continuity.append", "continuity.close"}

# What kind of fix an issue needs. "auto" classes may be fixed and shipped by the fix
# loop without asking; "approval" classes are prepared and wait for Nic.
FIX_CLASSES = {
    "client": "auto",       # client code/retry behavior (local agents)
    "guidance": "auto",     # skills, agent instructions, tool descriptions, docs
    "operator": "approval", # a live request/operation needs a human or agent decision
    "server": "approval",   # hub code, permissions, config or storage
}


@dataclass
class Finding:
    key: str
    title: str
    fix_class: str
    count: int
    refs: list[str] = field(default_factory=list)
    detail: str = ""


def _parse(ts: str) -> datetime:
    value = datetime.fromisoformat(ts)
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _ro(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def load_paths(config_file: Path) -> tuple[Path, Path | None]:
    """Return the GT database and Brain usage log paths from the service config."""
    with config_file.open("rb") as handle:
        config = tomllib.load(handle)
    database = Path(config["server"]["database"])
    brain = config.get("brain_core") or {}
    usage = Path(brain["decision_staging_dir"]) / "brain-usage.sqlite3" if brain.get("enabled") else None
    return database, usage


def brain_findings(usage_db: Path | None, since: datetime) -> tuple[list[Finding], dict]:
    if usage_db is None or not usage_db.exists():
        return [], {"calls": 0, "errors": 0, "by_actor": {}}
    with _ro(usage_db) as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM brain_usage WHERE at >= ?", (since.isoformat(),))]
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if row["outcome"] != "ok":
            code = row["error_code"] or "unknown"
            if code == "UNAUTHORIZED":
                groups[f"bc.unauthenticated:{row['operation']}"].append(row)
            else:
                groups[f"bc.error:{row['operation']}:{code}"].append(row)
        limit = SLOW_WRITE_MS if row["operation"] in BRAIN_WRITES else SLOW_READ_MS
        if row["duration_ms"] > limit:
            groups[f"bc.slow:{row['operation']}"].append(row)
    findings = []
    for key, items in sorted(groups.items()):
        kind = key.split(":", 1)[0]
        code = key.rsplit(":", 1)[-1]
        if kind == "bc.slow":
            op = key.split(":")[1]
            limit = SLOW_WRITE_MS if op in BRAIN_WRITES else SLOW_READ_MS
            title, fix_class = f"Slow Brain {op} (over {limit // 1000} s)", "server"
        elif kind == "bc.unauthenticated":
            title, fix_class = f"Brain {key.split(':')[1]} called without a valid login", "guidance"
        elif code in {"brain.invalid_argument", "UNSUPPORTED"}:
            title, fix_class = f"Agents send invalid arguments to Brain {key.split(':')[1]}", "guidance"
        elif code == "ACCESS_DENIED":
            title, fix_class = f"Brain {key.split(':')[1]} refused for access", "server"
        else:
            title, fix_class = f"Brain {key.split(':')[1]} fails with {code}", "server"
        actors = Counter(r["actor"] for r in items)
        slowest = max(r["duration_ms"] for r in items)
        findings.append(Finding(
            key, title, fix_class, len(items),
            refs=sorted({r["operation_id"] or r["record_id"] for r in items if r["operation_id"] or r["record_id"]})[:10],
            detail=f"agents: {dict(actors)}; slowest {slowest / 1000:.1f} s",
        ))
    stats = {
        "calls": len(rows),
        "errors": sum(1 for r in rows if r["outcome"] != "ok"),
        "by_actor": dict(Counter(r["actor"] for r in rows)),
    }
    return findings, stats


def gt_findings(database: Path, since: datetime, now: datetime) -> tuple[list[Finding], dict]:
    findings: list[Finding] = []
    with _ro(database) as db:
        open_rows = db.execute(
            "SELECT request_id, recipient_id, work_state, updated_at, recovery_hold FROM requests"
            f" WHERE mode='action_requested' AND {REQUEST_STILL_OPEN_SQL}"
            f" AND julianday({REQUEST_LAST_ACTIVITY_SQL}) > julianday(?) - 14", (now.isoformat(),)
        ).fetchall()
        groups: dict[str, list] = defaultdict(list)
        for r in open_rows:
            age = now - _parse(r["updated_at"])
            if r["recovery_hold"]:
                groups[f"gt.recovery_hold:{r['recipient_id']}"].append(r)
            elif r["work_state"] == "requested" and age > STALE_PICKUP:
                groups[f"gt.not_picked_up:{r['recipient_id']}"].append(r)
            elif r["work_state"] == "blocked" and age > STALE_WORK:
                groups[f"gt.blocked:{r['recipient_id']}"].append(r)
            elif r["work_state"] in {"acknowledged", "in_progress"} and age > STALE_WORK:
                groups[f"gt.stalled:{r['recipient_id']}"].append(r)
        titles = {
            "gt.recovery_hold": "Requests held after a restore, waiting for operator disposition",
            "gt.not_picked_up": "Requests to {} not picked up",
            "gt.blocked": "Requests to {} blocked for over a day",
            "gt.stalled": "Requests to {} started but no progress for over a day",
        }
        for key, items in sorted(groups.items()):
            kind, recipient = key.split(":", 1)
            oldest = min(_parse(r["updated_at"]) for r in items)
            findings.append(Finding(
                key, titles[kind].format(recipient), "operator", len(items),
                refs=[r["request_id"] for r in items][:10],
                detail=f"oldest untouched {(now - oldest).total_seconds() / 3600:.0f} h",
            ))

        for state, fix_class in (("uncertain", "operator"), ("failed", "server")):
            rows = db.execute(
                "SELECT operation_id, operation_kind, error_code FROM operations WHERE operation_state=?", (state,)
            ).fetchall()
            if rows:
                findings.append(Finding(
                    f"gt.send_{state}", f"GT sends in state '{state}'", fix_class, len(rows),
                    refs=[r["operation_id"] for r in rows][:10],
                    detail=str(dict(Counter(r["error_code"] or r["operation_kind"] for r in rows))),
                ))
        stuck = [r for r in db.execute(
            "SELECT operation_id, accepted_at FROM operations WHERE operation_state IN ('queued','sending')"
        ) if now - _parse(r["accepted_at"]) > STUCK_SEND]
        if stuck:
            findings.append(Finding("gt.send_stuck", "GT sends queued for over 30 minutes", "server",
                                    len(stuck), refs=[r["operation_id"] for r in stuck][:10]))

        events = Counter(r["event_type"] for r in db.execute(
            "SELECT event_type FROM events WHERE occurred_at >= ?", (since.isoformat(),)))
        fetch_failures = events["source_fetch_attempted"] - events["source_fetch_succeeded"]
        if fetch_failures > 0:
            findings.append(Finding("gt.source_fetch_failed", "GitHub conversation fetches failed", "server",
                                    fetch_failures, detail=f"{events['source_fetch_attempted']} attempted"))
        created = db.execute(
            "SELECT recipient_id, COUNT(*) FROM requests WHERE created_at >= ? GROUP BY recipient_id",
            (since.isoformat(),)).fetchall()
        returned = events["result_returned"]
    stats = {
        "requests_created": {r[0]: r[1] for r in created},
        "results_returned": returned,
        "open_requests": len(open_rows),
    }
    return findings, stats


class IssueStore:
    def __init__(self, path: Path, clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS issues (
                key TEXT PRIMARY KEY, title TEXT NOT NULL, fix_class TEXT NOT NULL,
                status TEXT NOT NULL, first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                occurrences INTEGER NOT NULL, last_run_count INTEGER NOT NULL,
                detail TEXT, refs TEXT, fix_ref TEXT, fix_note TEXT, fix_shipped_at TEXT,
                resolved_at TEXT, reopened INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS runs (
                run_at TEXT PRIMARY KEY, since TEXT NOT NULL, report TEXT NOT NULL
            );
            """
        )

    def last_run(self) -> datetime | None:
        row = self.db.execute("SELECT MAX(run_at) FROM runs").fetchone()
        return _parse(row[0]) if row[0] else None

    def apply(self, findings: list[Finding], now: datetime) -> list[dict]:
        seen = {f.key for f in findings}
        stamp = now.isoformat()
        for f in findings:
            row = self.db.execute("SELECT * FROM issues WHERE key=?", (f.key,)).fetchone()
            refs = json.dumps(f.refs)
            if row is None:
                self.db.execute(
                    "INSERT INTO issues(key,title,fix_class,status,first_seen,last_seen,occurrences,"
                    "last_run_count,detail,refs) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    (f.key, f.title, f.fix_class, "open", stamp, stamp, f.count, f.count, f.detail, refs))
                continue
            status, reopened = row["status"], row["reopened"]
            if status in {"fix_shipped", "resolved", "dismissed"}:
                status, reopened = "open", reopened + 1  # the fix did not hold
            self.db.execute(
                "UPDATE issues SET title=?, status=?, last_seen=?, occurrences=occurrences+?, last_run_count=?,"
                " detail=?, refs=?, reopened=?, resolved_at=NULL WHERE key=?",
                (f.title, status, stamp, f.count, f.count, f.detail, refs, reopened, f.key))
        for row in self.db.execute("SELECT * FROM issues WHERE status IN ('open','fix_shipped')").fetchall():
            if row["key"] in seen:
                continue
            self.db.execute("UPDATE issues SET last_run_count=0 WHERE key=?", (row["key"],))
            quiet_since = _parse(row["fix_shipped_at"] or row["last_seen"])
            if row["status"] == "fix_shipped" and now - quiet_since >= timedelta(days=RESOLVE_AFTER_DAYS):
                self.db.execute("UPDATE issues SET status='resolved', resolved_at=? WHERE key=?", (stamp, row["key"]))
            elif row["status"] == "open" and row["fix_class"] == "operator":
                # Live-state issues (stalled requests) clear themselves when the state clears.
                self.db.execute("UPDATE issues SET status='resolved', resolved_at=? WHERE key=?", (stamp, row["key"]))
        return self.issues()

    def issues(self) -> list[dict]:
        return [dict(r) | {"refs": json.loads(r["refs"] or "[]"), "fix_mode": FIX_CLASSES.get(r["fix_class"], "approval")}
                for r in self.db.execute("SELECT * FROM issues ORDER BY status, last_seen DESC")]

    def mark(self, key: str, *, shipped: str | None = None, dismiss: bool = False, note: str = "") -> None:
        """Record a shipped fix (verified later by absence) or dismiss a non-issue."""
        row = self.db.execute("SELECT status FROM issues WHERE key=?", (key,)).fetchone()
        if row is None:
            raise KeyError(key)
        if dismiss == bool(shipped):
            raise ValueError("give exactly one of shipped or dismiss")
        status = "dismissed" if dismiss else "fix_shipped"
        self.db.execute(
            "UPDATE issues SET status=?, fix_ref=?, fix_note=?, fix_shipped_at=? WHERE key=?",
            (status, shipped, note, self.clock().isoformat(), key))

    def record_run(self, now: datetime, since: datetime, report: dict) -> None:
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?)", (now.isoformat(), since.isoformat(), json.dumps(report)))
        cutoff = (now - timedelta(days=90)).isoformat()
        self.db.execute("DELETE FROM runs WHERE run_at < ?", (cutoff,))

    def runs_since(self, since: datetime) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT report FROM runs WHERE run_at >= ? ORDER BY run_at", (since.isoformat(),))]

    def close(self) -> None:
        self.db.close()


def alerts_for(brain: dict, findings: list[Finding], issues: list[dict]) -> list[str]:
    alerts = []
    if brain["errors"] >= ERROR_ALERT_THRESHOLD:
        alerts.append(f"{brain['errors']} Brain Core errors since the last run")
    for f in findings:
        if f.key in {"gt.send_uncertain", "gt.send_failed", "gt.send_stuck"}:
            alerts.append(f"{f.title}: {f.count}")
    for issue in issues:
        if issue["status"] == "open" and issue["reopened"] and issue["last_run_count"]:
            alerts.append(f"Fix did not hold, issue reopened: {issue['title']}")
    return alerts


def run(config_file: Path, state_dir: Path, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    database, usage = load_paths(config_file)
    store = IssueStore(state_dir / "triage.sqlite3", clock=lambda: now)
    try:
        since = store.last_run() or now - timedelta(days=1)
        bc, bc_stats = brain_findings(usage, since)
        gt, gt_stats = gt_findings(database, since, now)
        findings = bc + gt
        issues = store.apply(findings, now)
        report = {
            "schema": "gremlin-town.triage/v1",
            "run_at": now.isoformat(), "since": since.isoformat(),
            "brain": bc_stats, "gt": gt_stats,
            "findings": [asdict(f) for f in findings],
            "issues": issues,
            "alerts": alerts_for(bc_stats, findings, issues),
        }
        store.record_run(now, since, report)
    finally:
        store.close()
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "latest.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    (state_dir / "latest.txt").write_text(render_daily(report), encoding="utf-8")
    return report


def render_daily(report: dict) -> str:
    open_issues = [i for i in report["issues"] if i["status"] == "open"]
    lines = [f"GT + Brain Core triage, {report['run_at'][:16]} UTC (since {report['since'][:16]})",
             f"Brain calls: {report['brain']['calls']} ({report['brain']['errors']} errors). "
             f"GT requests created: {sum(report['gt']['requests_created'].values())}, "
             f"results returned: {report['gt']['results_returned']}, open: {report['gt']['open_requests']}."]
    if report["alerts"]:
        lines.append("ALERTS: " + "; ".join(report["alerts"]))
    lines.append(f"Open issues: {len(open_issues)}")
    for issue in open_issues:
        lines.append(f"- [{issue['fix_mode']}] {issue['title']} ({issue['key']}): {issue['last_run_count']} this run, "
                     f"{issue['occurrences']} total. {issue['detail'] or ''}".rstrip())
    return "\n".join(lines) + "\n"


def weekly(state_dir: Path, *, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    store = IssueStore(state_dir / "triage.sqlite3", clock=lambda: now)
    try:
        runs = store.runs_since(now - timedelta(days=7))
        issues = store.issues()
    finally:
        store.close()
    calls, errors, by_actor, created, returned = 0, 0, Counter(), Counter(), 0
    for r in runs:
        calls += r["brain"]["calls"]
        errors += r["brain"]["errors"]
        by_actor.update(r["brain"]["by_actor"])
        created.update(r["gt"]["requests_created"])
        returned += r["gt"]["results_returned"]
    week = (now - timedelta(days=7)).isoformat()
    shipped = [i for i in issues if (i["fix_shipped_at"] or "") >= week]
    resolved = [i for i in issues if (i["resolved_at"] or "") >= week]
    still_open = [i for i in issues if i["status"] == "open"]
    rate = f"{errors / calls:.0%}" if calls else "n/a"
    lines = [
        f"Weekly GT + Brain Core summary to {now.date()} ({len(runs)} daily runs)",
        f"Brain Core: {calls} calls, error rate {rate}. By agent: "
        + (", ".join(f"{a} {n}" for a, n in by_actor.most_common()) or "none"),
        f"Gremlin Town: {sum(created.values())} requests created ("
        + (", ".join(f"to {a} {n}" for a, n in created.most_common()) or "none") + f"), {returned} results returned.",
        f"Fixes shipped: {len(shipped)}. Confirmed fixed: {len(resolved)}. Still open: {len(still_open)}.",
    ]
    for i in still_open[:5]:
        lines.append(f"- open: {i['title']} ({i['occurrences']} total)")
    for i in resolved:
        lines.append(f"- fixed: {i['title']}")
    return "\n".join(lines) + "\n"
