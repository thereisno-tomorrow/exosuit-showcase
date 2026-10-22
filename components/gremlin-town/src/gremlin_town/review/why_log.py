"""The "why it came up" log: every surfacing's full reason, kept 90 days (D8, §7).

Written before sending: `run.py` writes surfacing rows, builds the message from
those rows, then hands it to Diablo. A write failure means nothing is sent and
the run fails loudly - the reason is never rebuilt after the fact. Beyond the
spec's own column list, each surfacing also keeps `record_state` (the item's
state when it was surfaced), which `brain_core.moments.returnable` needs to
tell a "waiting/parked -> open" return from an unrelated one.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
import errno
import json
import os
import sqlite3
import threading
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Mapping

from brain_core.review import HistoryRow, MorningBootstrap
from brain_core.timewords import SINGAPORE, today_in_singapore, personal_today

RETENTION_DAYS = 90
_REQUEST_ONCE_RULES = ("request_returned_proof", "stale_request")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS surfacings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    surfaced_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    signal TEXT,
    delivery_ref TEXT,
    item_ref TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    record_path TEXT,
    record_state TEXT,
    fields_used TEXT NOT NULL,
    rule TEXT NOT NULL,
    assumptions TEXT NOT NULL,
    unknowns TEXT NOT NULL,
    response TEXT,
    response_words TEXT,
    responded_at TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    at TEXT NOT NULL,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS busy_calendar_operations (
    operation_key TEXT PRIMARY KEY,
    entry_key TEXT NOT NULL,
    intent TEXT NOT NULL,
    state TEXT NOT NULL,
    result TEXT
);
CREATE TABLE IF NOT EXISTS request_review_consumption (
    item_ref TEXT NOT NULL,
    rule TEXT NOT NULL,
    surfaced_at TEXT NOT NULL,
    PRIMARY KEY (item_ref, rule)
);
CREATE TABLE IF NOT EXISTS work_calendar_effects (
    owner_locator TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    caller TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    operation_hash TEXT NOT NULL,
    brain_commit TEXT NOT NULL,
    calendar TEXT NOT NULL,
    PRIMARY KEY (owner_locator, owner_id, caller, operation_id, operation_hash, brain_commit)
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_event_cursor TEXT,
    outcome TEXT
);
CREATE TABLE IF NOT EXISTS morning_bootstrap (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    input_sha TEXT NOT NULL,
    first_ids TEXT NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS shown_item_refs (
    item_ref TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS morning_packets (
    day TEXT PRIMARY KEY,
    prepared_at TEXT NOT NULL,
    request TEXT NOT NULL,
    refs TEXT NOT NULL,
    short TEXT NOT NULL,
    bootstrap_input_sha TEXT,
    is_empty INTEGER NOT NULL,
    completed_at TEXT
);
CREATE TABLE IF NOT EXISTS name_requests (
    day TEXT NOT NULL,
    item_ref TEXT NOT NULL,
    record_sha256 TEXT NOT NULL,
    prepared_at TEXT NOT NULL,
    request TEXT NOT NULL,
    request_id TEXT,
    PRIMARY KEY (day, item_ref)
);
CREATE TABLE IF NOT EXISTS placement_notices (
    day TEXT NOT NULL,
    key TEXT NOT NULL,
    title TEXT NOT NULL,
    noted_at TEXT NOT NULL,
    sent_at TEXT,
    PRIMARY KEY (day, key)
);
CREATE TABLE IF NOT EXISTS placement_lock (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    owner TEXT,
    taken_at TEXT
);
CREATE TABLE IF NOT EXISTS placement_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    ran_at TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    changes_seen INTEGER NOT NULL,
    plan TEXT NOT NULL,
    outcome TEXT NOT NULL,
    error TEXT
);
"""


class WhyLogError(RuntimeError):
    """A why-log write failed; the caller must send nothing and fail loudly."""


@dataclass(frozen=True, slots=True)
class Surfacing:
    surfaced_at: datetime
    channel: str
    item_ref: str
    record_sha256: str
    rule: str
    fields_used: Mapping[str, Any]
    signal: str | None = None
    delivery_ref: str | None = None
    record_path: str | None = None
    record_state: str | None = None
    assumptions: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    question: str | None = None
    offered: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedMorning:
    """Durable exact delivery facts for one morning key, before request creation.

    ``request`` holds the complete deliver arguments, including its stable operation
    key and full packet text/constraints. References name only its logged line batch.
    The original timestamp is used when recording actual successful acceptance.
    """

    prepared_at: datetime
    request: Mapping[str, Any]
    references: tuple[str, ...]
    short: str
    bootstrap_input_sha: str | None
    is_empty: bool
    completed: bool = False


@dataclass(frozen=True, slots=True)
class AnswerContext:
    """Stored authority for a referenced review line; never caller-supplied data.

    ``thing`` is its first ID, ``day`` its Singapore work day, and ``open`` says
    whether it remains unanswered. Offered moves bound the answer's authority.
    """

    reference: str
    question: str
    offered: tuple[str, ...]
    thing: str
    day: date
    open: bool
    marks: tuple[str, ...] = ()
    suggested_name: str | None = None
    suggested_title: str | None = None
    suggested_label: str | None = None
    tidy_suggestion: bool = False
    record_sha256: str | None = None
    length_choices: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class FullReason:
    """Every stored column for one surfacing row ("What's the full reason for 3?")."""

    item_ref: str
    surfaced_at: datetime
    channel: str
    rule: str
    fields_used: Mapping[str, Any]
    assumptions: tuple[str, ...]
    unknowns: tuple[str, ...]
    record_sha256: str
    record_path: str | None
    signal: str | None
    delivery_ref: str | None
    response: str | None
    response_words: str | None
    responded_at: datetime | None


@dataclass(frozen=True, slots=True)
class SignalRecord:
    """One row from the `signals` table (a reported occurrence, e.g. `pc_on`)."""

    id: int
    kind: str
    actor: str
    at: datetime
    detail: str | None


def _row_to_history(row: sqlite3.Row) -> HistoryRow:
    return HistoryRow(
        item_ref=row["item_ref"],
        channel=row["channel"],
        rule=row["rule"],
        record_sha256=row["record_sha256"],
        record_state=row["record_state"],
        surfaced_at=datetime.fromisoformat(row["surfaced_at"]),
        response=row["response"],
        responded_at=datetime.fromisoformat(row["responded_at"]) if row["responded_at"] else None,
        record_path=row["record_path"],
    )


def _row_to_full_reason(row: sqlite3.Row) -> FullReason:
    return FullReason(
        item_ref=row["item_ref"],
        surfaced_at=datetime.fromisoformat(row["surfaced_at"]),
        channel=row["channel"],
        rule=row["rule"],
        fields_used=json.loads(row["fields_used"]),
        assumptions=tuple(json.loads(row["assumptions"])),
        unknowns=tuple(json.loads(row["unknowns"])),
        record_sha256=row["record_sha256"],
        record_path=row["record_path"],
        signal=row["signal"],
        delivery_ref=row["delivery_ref"],
        response=row["response"],
        response_words=row["response_words"],
        responded_at=datetime.fromisoformat(row["responded_at"]) if row["responded_at"] else None,
    )


class WhyLog:
    """A SQLite file next to GT, kept 90 days (`why_log_file` in `[review]`)."""

    def calendar_effect(
        self, *, owner_locator: str, owner_id: str, caller: str,
        operation_id: str, operation_hash: str, brain_commit: str,
    ) -> dict[str, object] | None:
        """Read a non-expiring successful calendar acknowledgment for an exact
        authenticated owner/caller, work namespace, public hash and Brain commit.

        The host must verify the accepted batch before reading this derivative
        service metadata. Check-only and replay-marker calls never consult it.
        """
        key = self._calendar_effect_key(owner_locator, owner_id, caller, operation_id, operation_hash, brain_commit)
        try:
            row = self._connection.execute("""SELECT calendar FROM work_calendar_effects
                WHERE owner_locator=? AND owner_id=? AND caller=? AND operation_id=? AND operation_hash=? AND brain_commit=?""",
                key).fetchone()
            return None if row is None else self._calendar_effect_block(json.loads(row["calendar"]))
        except (sqlite3.Error, ValueError, TypeError) as exc:
            raise WhyLogError("could not read the calendar acknowledgment") from exc

    def record_calendar_effect(
        self, *, owner_locator: str, owner_id: str, caller: str,
        operation_id: str, operation_hash: str, brain_commit: str,
        calendar: Mapping[str, object],
    ) -> dict[str, object]:
        """Persist the first actual successful calendar acknowledgment atomically.

        Return the exact first acknowledgment on repeated calls; refuse failures,
        guest-blocked effects and conflicting acknowledgments. Existing WhyLog
        retention never expires these derivative operation receipts.
        """
        key = self._calendar_effect_key(owner_locator, owner_id, caller, operation_id, operation_hash, brain_commit)
        block = self._calendar_effect_block(dict(calendar))
        serialized = json.dumps(block, sort_keys=True, separators=(",", ":"), allow_nan=False)
        try:
            with self._connection:
                self._connection.execute("INSERT OR IGNORE INTO work_calendar_effects VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (*key, serialized))
                row = self._connection.execute("""SELECT calendar FROM work_calendar_effects
                    WHERE owner_locator=? AND owner_id=? AND caller=? AND operation_id=? AND operation_hash=? AND brain_commit=?""",
                    key).fetchone()
                if row is None or row["calendar"] != serialized:
                    raise WhyLogError("the operation already has a different successful calendar acknowledgment")
                return self._calendar_effect_block(json.loads(row["calendar"]))
        except sqlite3.Error as exc:
            raise WhyLogError("could not record the calendar acknowledgment") from exc

    def _calendar_effect_key(self, owner_locator, owner_id, caller, operation_id, operation_hash, brain_commit):
        import re
        if (any(not isinstance(value, str) or not value.strip() for value in (owner_locator, owner_id, caller, operation_id))
            or not isinstance(operation_hash, str) or re.fullmatch(r"[0-9a-f]{64}", operation_hash) is None
            or not isinstance(brain_commit, str) or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", brain_commit) is None):
            raise WhyLogError("invalid exact calendar acknowledgment key")
        return owner_locator, owner_id, caller, operation_id, operation_hash, brain_commit

    def _calendar_effect_block(self, calendar):
        if (not isinstance(calendar, dict) or calendar.get("status") not in ("added", "moved", "on_calendar", "ask_remove", "kept")
            or not isinstance(calendar.get("text"), str) or not calendar["text"]):
            raise WhyLogError("only an actual successful calendar acknowledgment may be recorded")
        return calendar

    def __init__(self, path: str | Path) -> None:
        self._schedule_guard = threading.Lock()
        self._path = Path(path)
        try:
            if str(self._path) != ":memory:":
                self._path.parent.mkdir(parents=True, exist_ok=True)
            # One connection, used from the event loop and the signal route's worker threads: every
            # method holds this lock, so no two use it at once (placement-02).
            self._lock = threading.RLock()
            self._connection = sqlite3.connect(str(self._path), check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.executescript(_SCHEMA)
            columns = {row[1] for row in self._connection.execute("PRAGMA table_info(surfacings)")}
            for name, definition in (("question", "TEXT"), ("offered", "TEXT NOT NULL DEFAULT '[]'")):
                if name not in columns:
                    self._connection.execute(f"ALTER TABLE surfacings ADD COLUMN {name} {definition}")
            # Preserve already delivered W4 lines when opening an existing log.
            # Failed deliveries have no sent run and must remain eligible for retry.
            self._connection.executemany(
                "INSERT OR IGNORE INTO request_review_consumption (item_ref, rule, surfaced_at) VALUES (?, ?, ?)",
                ((row["item_ref"], row["rule"], row["surfaced_at"]) for row in self._delivered_review_rows()
                 if row["rule"] in _REQUEST_ONCE_RULES),
            )
            self._retain_shown_refs()
            self._connection.commit()
        except (OSError, sqlite3.Error) as exc:
            raise WhyLogError(f"could not open the why-log at {self._path}") from exc

    def close(self) -> None:
        self._connection.close()

    def busy_change(self, operation_key: str) -> dict[str, Any] | None:
        """Read immutable busy intent before resolving relative clock times on a retry."""
        try:
            row = self._connection.execute("SELECT intent,state FROM busy_calendar_operations WHERE operation_key=?",
                                           (operation_key,)).fetchone()
            return {"intent": json.loads(row["intent"]), "state": row["state"]} if row is not None else None
        except sqlite3.Error as exc:
            raise WhyLogError("could not read the busy calendar operation") from exc

    def prepare_busy_change(self, operation_key: str, entry_key: str, intent: list[str]) -> dict[str, Any]:
        """Freeze busy-entry intent before Google; serialize unresolved edits to one entry."""
        serialized = json.dumps(intent, separators=(",", ":"))
        try:
            with self._lock:
                self._connection.execute("BEGIN IMMEDIATE")
                try:
                    row = self._connection.execute("SELECT * FROM busy_calendar_operations WHERE operation_key=?",
                                                   (operation_key,)).fetchone()
                    if row is None:
                        pending = self._connection.execute(
                            "SELECT 1 FROM busy_calendar_operations WHERE entry_key=? AND state!='done'", (entry_key,)
                        ).fetchone()
                        if pending is not None:
                            self._connection.commit()
                            return {"state": "blocked"}
                        self._connection.execute("INSERT INTO busy_calendar_operations VALUES (?,?,?,'prepared',NULL)",
                                                 (operation_key, entry_key, serialized))
                        row = self._connection.execute("SELECT * FROM busy_calendar_operations WHERE operation_key=?",
                                                       (operation_key,)).fetchone()
                    if row["entry_key"] != entry_key or row["intent"] != serialized:
                        raise WhyLogError("busy calendar operation conflicts with its saved intent")
                    self._connection.commit()
                    return {"state": row["state"], "result": json.loads(row["result"]) if row["result"] else None}
                except BaseException:
                    self._connection.rollback()
                    raise
        except sqlite3.Error as exc:
            raise WhyLogError("could not prepare the busy calendar change") from exc

    def attempt_busy_change(self, operation_key: str) -> bool:
        """Exactly one process may attempt a prepared mutation; an uncertain attempt stays held."""
        try:
            with self._lock, self._connection:
                cursor = self._connection.execute(
                    "UPDATE busy_calendar_operations SET state='attempted' WHERE operation_key=? AND state='prepared'",
                    (operation_key,),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise WhyLogError("could not record the busy calendar attempt") from exc

    def complete_busy_change(self, operation_key: str, result: dict[str, Any]) -> None:
        try:
            with self._lock, self._connection:
                cursor = self._connection.execute(
                    "UPDATE busy_calendar_operations SET state='done',result=? WHERE operation_key=?",
                    (json.dumps(result, sort_keys=True), operation_key),
                )
                if cursor.rowcount != 1:
                    raise WhyLogError("busy calendar operation is missing")
        except sqlite3.Error as exc:
            raise WhyLogError("could not record the busy calendar result") from exc

    def prepare_name_request(self, day: date, item_ref: str, record_sha256: str, now: datetime,
                             request: Mapping[str, Any]) -> dict[str, Any]:
        """Freeze the exact request before send; changed context cannot change a same-day retry."""
        try:
            with self._lock, self._connection:
                self._connection.execute("""INSERT OR IGNORE INTO name_requests
                    (day,item_ref,record_sha256,prepared_at,request) VALUES (?,?,?,?,?)""",
                    (day.isoformat(), item_ref, record_sha256, now.isoformat(), json.dumps(dict(request), sort_keys=True)))
                row = self._connection.execute("SELECT * FROM name_requests WHERE day=? AND item_ref=?",
                                               (day.isoformat(), item_ref)).fetchone()
            return dict(row, request=json.loads(row["request"]))
        except sqlite3.Error as exc:
            raise WhyLogError("could not prepare the name request") from exc

    def name_requests(self, day: date) -> tuple[dict[str, Any], ...]:
        try:
            with self._lock:
                rows = self._connection.execute("SELECT * FROM name_requests WHERE day=? ORDER BY item_ref",
                                               (day.isoformat(),)).fetchall()
            return tuple(dict(row, request=json.loads(row["request"])) for row in rows)
        except sqlite3.Error as exc:
            raise WhyLogError("could not read the name requests") from exc

    def record_name_request(self, day: date, item_ref: str, request_id: str) -> None:
        if not request_id:
            raise WhyLogError("name request receipt has no request ID")
        try:
            with self._lock, self._connection:
                self._connection.execute("UPDATE name_requests SET request_id=? WHERE day=? AND item_ref=?",
                                         (request_id, day.isoformat(), item_ref))
        except sqlite3.Error as exc:
            raise WhyLogError("could not record the name request") from exc

    def answer_context(self, reference: str) -> AnswerContext | None:
        """Read one exact line reference with its question, offers and open state."""
        if not isinstance(reference, str) or not reference.isdecimal():
            return None
        row = self._connection.execute("SELECT * FROM surfacings WHERE id=?", (int(reference),)).fetchone()
        if row is None or not row["question"]:
            return None
        offered = json.loads(row["offered"])
        if not isinstance(offered, list) or not all(isinstance(move, str) for move in offered):
            return None
        fields = json.loads(row["fields_used"])
        marks = fields.get("review_marks", ())
        return AnswerContext(reference=str(row["id"]), question=row["question"],
            offered=tuple(offered), thing=row["item_ref"],
            day=today_in_singapore(datetime.fromisoformat(row["surfaced_at"])),
            open=row["responded_at"] is None, suggested_name=fields.get("suggested_name"),
            suggested_title=fields.get("suggested_title"), suggested_label=fields.get("suggested_label"),
            tidy_suggestion=fields.get("tidy_suggestion", False),
            record_sha256=row["record_sha256"],
            length_choices=tuple(fields.get("length_choices", ())),
            marks=tuple(mark if isinstance(mark, str) else mark["mark"]
                for mark in marks if isinstance(mark, str) or isinstance(mark, Mapping) and isinstance(mark.get("mark"), str)))

    def review_references(self, now: datetime) -> tuple[str, ...]:
        """Exact numeric references for logged review rows at this run's timestamp, in ID order.

        This read does not consume request markers or include weekly/other-channel rows.
        """
        try:
            rows = self._connection.execute(
                "SELECT id FROM surfacings WHERE channel='review' AND surfaced_at=? ORDER BY id",
                (now.isoformat(),),
            ).fetchall()
        except sqlite3.Error as exc:
            raise WhyLogError("could not read this review's line references") from exc
        return tuple(str(row["id"]) for row in rows)

    def last_sent_review_at(self) -> datetime | None:
        """The actual latest successful review run timestamp, or None with no such run.

        Read existing runs only; failed/aborted and other-kind runs do not advance it.
        """
        try:
            rows = self._connection.execute(
                "SELECT started_at FROM runs WHERE kind='review' AND outcome='sent'",
            ).fetchall()
        except sqlite3.Error as exc:
            raise WhyLogError("could not read the last successful review run") from exc
        return max((datetime.fromisoformat(row["started_at"]) for row in rows), default=None)

    def stage_morning_bootstrap(self, *, input_sha: str, first_ids: Iterable[str]) -> None:
        """Idempotently stage ALL first IDs from the actual pinned pre-move snapshot.

        W18 supplies these facts; this method never guesses a clock or creates review history.
        A different snapshot/inventory cannot replace the once-only bootstrap.
        """
        if isinstance(first_ids, (str, bytes)):
            raise ValueError("first_ids must be an inventory of first IDs, not a string")
        ids = frozenset(first_ids)
        if (not isinstance(input_sha, str) or len(input_sha) not in (40, 64)
                or any(c not in "0123456789abcdef" for c in input_sha)):
            raise ValueError("input_sha must be the actual pinned input SHA (40 or 64 lowercase hex characters)")
        if any(not isinstance(ref, str) or not ref.strip() for ref in ids):
            raise ValueError("first_ids must contain nonempty first IDs from the pinned input")
        encoded = json.dumps(sorted(ids))
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT OR IGNORE INTO morning_bootstrap (id, input_sha, first_ids) VALUES (1, ?, ?)",
                    (input_sha, encoded),
                )
                row = self._connection.execute("SELECT * FROM morning_bootstrap WHERE id=1").fetchone()
                if row["input_sha"] != input_sha or row["first_ids"] != encoded:
                    raise ValueError("morning bootstrap is already pinned to a different input SHA or inventory")
        except sqlite3.Error as exc:
            raise WhyLogError("could not stage the pinned morning bootstrap") from exc

    def morning_bootstrap(self) -> MorningBootstrap | None:
        """Read pinned bootstrap facts and completion; None means an absent prerequisite.

        Completed inventory remains available so a new version never makes an old identity new.
        """
        try:
            row = self._connection.execute("SELECT * FROM morning_bootstrap WHERE id=1").fetchone()
        except sqlite3.Error as exc:
            raise WhyLogError("could not read the pinned morning bootstrap") from exc
        return (MorningBootstrap(row["input_sha"], frozenset(json.loads(row["first_ids"])),
                                 row["completed_at"] is not None) if row else None)

    def morning_bootstrap_completed(self) -> bool:
        """Whether a successful sent new-style morning atomically consumed bootstrap."""
        facts = self.morning_bootstrap()
        return facts is not None and facts.completed

    def shown_item_refs(self) -> frozenset[str]:
        """Actually shown references retained across prune; collector maps physical IDs to first IDs."""
        try:
            return frozenset(row["item_ref"] for row in self._connection.execute("SELECT item_ref FROM shown_item_refs"))
        except sqlite3.Error as exc:
            raise WhyLogError("could not read actually shown item references") from exc

    def morning_history(self) -> tuple[HistoryRow, ...]:
        """Review history for real deliveries, preserving W4 durable request markers.

        Aborted logging must not manufacture carried counts or said-once facts.
        """
        try:
            delivered = {row["id"] for row in self._delivered_review_rows()}
            rows = self._connection.execute("SELECT * FROM surfacings ORDER BY surfaced_at, id").fetchall()
            history = tuple(_row_to_history(row) for row in rows
                if not (row["channel"] == "review" and row["rule"] in _REQUEST_ONCE_RULES)
                and not (row["channel"] == "weekly" and row["rule"] == "stale_request")
                and (row["channel"] != "review" or row["id"] in delivered))
            markers = tuple(row for row in self.request_review_history()
                            if row.channel == "review" and row.rule in _REQUEST_ONCE_RULES)
            return self._current_card_picks(history + markers)
        except sqlite3.Error as exc:
            raise WhyLogError("could not read delivered morning history") from exc

    def prepare_morning(self, packet: PreparedMorning) -> PreparedMorning:
        """Durably freeze one day's exact packet before request creation.

        A previously prepared day wins unchanged, including its refs and timestamp;
        retries must use those facts rather than derive another input for the key.
        This preparation creates no sent run or consumed bootstrap/request marker.
        """
        day = today_in_singapore(packet.prepared_at)
        try:
            with self._connection:
                existing = self.morning_packet(day)
                if existing is not None:
                    return existing
                if packet.completed or packet.request.get("client_operation_id") != f"review-{day.isoformat()}":
                    raise WhyLogError("a prepared morning must have its day's review key and no successful completion")
                rows = self.review_references(packet.prepared_at)
                if len(set(packet.references)) != len(packet.references) or not set(packet.references).issubset(rows):
                    raise WhyLogError("a prepared morning must reference its exact already logged review rows")
                self._connection.execute(
                    """INSERT OR IGNORE INTO morning_packets
                    (day, prepared_at, request, refs, short, bootstrap_input_sha, is_empty)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (day.isoformat(), packet.prepared_at.isoformat(), json.dumps(dict(packet.request), sort_keys=True),
                     json.dumps(packet.references), packet.short, packet.bootstrap_input_sha, int(packet.is_empty)),
                )
                return self.morning_packet(day)
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise WhyLogError("could not prepare the exact morning packet") from exc

    def morning_packet(self, day: date) -> PreparedMorning | None:
        """Read a day's exact prepared packet, including successful completion state."""
        try:
            row = self._connection.execute("SELECT * FROM morning_packets WHERE day=?", (day.isoformat(),)).fetchone()
            return (PreparedMorning(datetime.fromisoformat(row["prepared_at"]), json.loads(row["request"]),
                                    tuple(json.loads(row["refs"])), row["short"], row["bootstrap_input_sha"],
                                    bool(row["is_empty"]), row["completed_at"] is not None) if row else None)
        except sqlite3.Error as exc:
            raise WhyLogError("could not read the exact morning packet") from exc

    def _delivered_review_rows(self) -> tuple[sqlite3.Row, ...]:
        # A timestamp can contain abandoned or concurrent batches. A prepared
        # request proves only its exact references; old deliveries without a
        # prepared packet retain their existing timestamp-based semantics.
        batches = {row["prepared_at"]: set(json.loads(row["refs"])) for row in self._connection.execute(
            "SELECT prepared_at, refs FROM morning_packets")}
        rows = self._connection.execute("""SELECT s.* FROM surfacings s WHERE s.channel='review'
            AND (s.signal='review_part' OR EXISTS (SELECT 1 FROM runs r WHERE r.kind='review' AND r.outcome='sent'
                        AND r.started_at=s.surfaced_at)) ORDER BY s.surfaced_at, s.id""").fetchall()
        return tuple(row for row in rows if row["surfaced_at"] not in batches
                     or str(row["id"]) in batches[row["surfaced_at"]] or row["signal"] == 'review_part')

    def current_part_lines(self, part: str, lines, *, now: datetime, original=(), refresh_pending=False, review_day=None) -> tuple[dict[str, Any], ...]:
        """Log a synchronous part before returning it; reuse today's same-part references."""
        today = review_day if review_day is not None else personal_today(now)
        original_refs = {line["reference"] for line in original if line["part"] == part}
        try:
            with self._connection:
                saved = self._connection.execute("SELECT * FROM surfacings WHERE channel='review' ORDER BY id").fetchall()
                saved = [row for row in saved if json.loads(row["fields_used"]).get("card_review_day", personal_today(datetime.fromisoformat(row["surfaced_at"])).isoformat()) == today.isoformat()
                         and (str(row["id"]) in original_refs or json.loads(row["fields_used"]).get("review_part") == part)]
                result = []
                for line in lines:
                    if line.item_ref is None:
                        result.append(dict(reference=None, kind=line.rule, part=part, number=line.number,
                            item_ref=None, text=line.text, allowed_answers=[], offered=[],
                            answered=False, response=None, response_words=None, already_picked=False))
                        continue
                    fields = dict(line.fields_used, review_part=part, number=line.number,
                                  allowed_answers=list(line.allowed_answers))
                    if review_day is not None:
                        fields["card_review_day"] = review_day.isoformat()
                    row = next((row for row in reversed(saved) if row["item_ref"] == line.item_ref and row["rule"] == line.rule), None)
                    if refresh_pending and row is not None and row["record_sha256"] != line.record_sha256:
                        # Keep the old shown question immutable for exact retries.
                        # The next question gets its own authority and version.
                        fields["refreshes_reference"] = str(row["id"])
                        if row["responded_at"] is None:
                            self._connection.execute("UPDATE surfacings SET response='refreshed', responded_at=? WHERE id=?",
                                                     (now.isoformat(), row["id"]))
                        row = None
                    elif row is not None and (previous := json.loads(row["fields_used"]).get("refreshes_reference")):
                        fields["refreshes_reference"] = previous
                    values = (line.record_sha256 or "", line.record_path, line.record_state, json.dumps(fields, sort_keys=True),
                              line.text, json.dumps(list(line.offered)))
                    if row is None:
                        cursor = self._connection.execute("""INSERT INTO surfacings
                            (surfaced_at,channel,signal,item_ref,rule,record_sha256,record_path,record_state,fields_used,
                             question,offered,assumptions,unknowns) VALUES (?,'review','review_part',?,?,?,?,?,?,?,?,?,?)""",
                            (now.isoformat(), line.item_ref, line.rule, *values,
                             json.dumps(list(line.assumptions)), json.dumps(list(line.unknowns))))
                        reference = str(cursor.lastrowid)
                    else:
                        reference = str(row["id"])
                        self._connection.execute("""UPDATE surfacings SET record_sha256=?,record_path=?,record_state=?,
                            fields_used=?,question=?,offered=? WHERE id=?""", (*values, row["id"]))
                    result.append(dict(reference=reference, kind=line.rule, part=part, number=line.number, item_ref=line.item_ref,
                                       suggested_name=line.suggested_name,
                                       suggested_title=line.suggested_title, suggested_label=line.suggested_label,
                                       text=line.text, allowed_answers=list(line.allowed_answers), offered=list(line.offered),
                                       answered=bool(row and row["responded_at"]),
                                       response=row["response"] if row else None, response_words=row["response_words"] if row else None,
                                       already_picked=bool(line.fields_used.get("preselected"))))
                self._retain_shown_refs()
            return tuple(result)
        except sqlite3.Error as exc:
            raise WhyLogError("could not log the current review part") from exc

    def review_part_state(self, part: str, *, original, now: datetime, review_day=None) -> tuple[dict[str, Any], ...]:
        """The questions actually put to him today, with their current saved answers."""
        refs = {line["reference"]: line for line in original if line["part"] == part}
        try:
            rows = self._connection.execute("SELECT * FROM surfacings WHERE channel='review' ORDER BY id").fetchall()
            refreshed = {json.loads(row["fields_used"]).get("refreshes_reference") for row in rows}
            result = []
            for row in rows:
                fields = json.loads(row["fields_used"])
                ref = str(row["id"])
                if ref in refreshed:
                    continue
                if fields.get("card_review_day", personal_today(datetime.fromisoformat(row["surfaced_at"])).isoformat()) != (review_day if review_day is not None else personal_today(now)).isoformat():
                    continue
                if ref not in refs and fields.get("review_part") != part:
                    continue
                source = refs.get(ref, {})
                result.append(dict(reference=ref, kind=row["rule"], part=part, number=fields.get("number", source.get("number", 0)),
                    suggested_name=fields.get("suggested_name"),
                    suggested_title=fields.get("suggested_title"), suggested_label=fields.get("suggested_label"),
                    item_ref=row["item_ref"], text=row["question"], rule=row["rule"],
                    allowed_answers=fields.get("allowed_answers", source.get("allowed_answers", [])),
                    offered=json.loads(row["offered"]), answered=row["responded_at"] is not None,
                    response=row["response"], response_words=row["response_words"],
                    already_picked=bool(fields.get("preselected"))))
            return tuple(result)
        except sqlite3.Error as exc:
            raise WhyLogError("could not read the current review answers") from exc

    def _retain_shown_refs(self) -> None:
        # Other reader channels answer synchronously. Delivery channels require a
        # successful run at the exact logged timestamp; aborted attempts are not shown.
        self._connection.execute("""INSERT OR IGNORE INTO shown_item_refs (item_ref)
            SELECT s.item_ref FROM surfacings s WHERE s.channel NOT IN ('review', 'moment', 'weekly')
            OR EXISTS (SELECT 1 FROM runs r WHERE r.started_at=s.surfaced_at AND
                ((s.channel='moment' AND r.kind='moments'
                     AND (r.outcome LIKE '%:sent' OR r.outcome LIKE '%:sent:couldnt_read'))
                 OR (s.channel='weekly' AND r.kind='weekly' AND r.outcome='answered')))""")
        self._connection.executemany("INSERT OR IGNORE INTO shown_item_refs (item_ref) VALUES (?)",
                                     ((row["item_ref"],) for row in self._delivered_review_rows()))

    def mark_today_answered(
        self, first_ids: Iterable[str], *, message: str, now: datetime,
        pending_rules: Mapping[str, set[str]] | None = None,
        validated_references: Iterable[str] = (),
    ) -> None:
        """Mark today's review lines answered by first ID after accepted publication.

        Validated calls and refused changes never mark a line answered.
        """
        ids = set(first_ids)
        references = set(validated_references)
        today = today_in_singapore(now)
        try:
            with self._connection:
                rows = self._connection.execute(
                    "SELECT id, item_ref, surfaced_at, rule FROM surfacings WHERE responded_at IS NULL AND channel='review'"
                ).fetchall()
                matched = [(message, now.isoformat(), row["id"]) for row in rows
                    if row["item_ref"] in ids and (today_in_singapore(datetime.fromisoformat(row["surfaced_at"])) == today
                        or str(row["id"]) in references)
                    and row["rule"] not in (pending_rules or {}).get(row["item_ref"], ())]
                self._connection.executemany(
                    "UPDATE surfacings SET response='answered', response_words=?, responded_at=? WHERE id=?", matched)
        except sqlite3.Error as exc:
            raise WhyLogError("could not mark today's review lines answered") from exc

    def record_surfacings(self, surfacings: Iterable[Surfacing]) -> None:
        """Write every surfacing row; raises before anything is sent on failure (§7)."""

        rows = list(surfacings)
        if not rows:
            return
        try:
            with self._connection:
                self._connection.executemany(
                    """INSERT INTO surfacings
                    (surfaced_at, channel, signal, delivery_ref, item_ref, record_sha256,
                     record_path, record_state, fields_used, rule, assumptions, unknowns, question, offered)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            entry.surfaced_at.isoformat(),
                            entry.channel,
                            entry.signal,
                            entry.delivery_ref,
                            entry.item_ref,
                            entry.record_sha256,
                            entry.record_path,
                            entry.record_state,
                            json.dumps(dict(entry.fields_used), sort_keys=True),
                            entry.rule,
                            json.dumps(list(entry.assumptions)),
                            json.dumps(list(entry.unknowns)),
                            entry.question,
                            json.dumps(list(entry.offered), sort_keys=True),
                        )
                        for entry in rows
                    ],
                )
                self._retain_shown_refs()
        except sqlite3.Error as exc:
            raise WhyLogError("could not write surfacing rows") from exc

    def record_response(
        self,
        *,
        item_ref: str,
        response: str,
        response_words: str | None,
        responded_at: datetime,
        channel: str | None = None,
    ) -> None:
        """Attach Nic's answer to the most recent surfacing of one item (drives §7's dedup).

        With ``channel``, only that channel's rows count: an answer to a "what next?" pick lands
        on its own `orient` row, never on a later "where am I?" (`overview`) row for the item."""

        query = "SELECT id FROM surfacings WHERE item_ref=?"
        parameters: tuple[str, ...] = (item_ref,)
        if channel is not None:
            query += " AND channel=?"
            parameters += (channel,)
        try:
            with self._connection:
                row = self._connection.execute(
                    query + " ORDER BY surfaced_at DESC, id DESC LIMIT 1", parameters,
                ).fetchone()
                if row is None:
                    raise WhyLogError(f"no surfacing found for {item_ref!r} to respond to")
                self._connection.execute(
                    "UPDATE surfacings SET response=?, response_words=?, responded_at=? WHERE id=?",
                    (response, response_words, responded_at.isoformat(), row["id"]),
                )
        except sqlite3.Error as exc:
            raise WhyLogError("could not write a surfacing response") from exc

    def record_signal(self, *, kind: str, actor: str, at: datetime, detail: str | None = None) -> None:
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO signals (kind, actor, at, detail) VALUES (?, ?, ?, ?)",
                    (kind, actor, at.isoformat(), detail),
                )
        except sqlite3.Error as exc:
            raise WhyLogError("could not write a signal row") from exc

    def record_run(
        self, *, kind: str, started_at: datetime, last_event_cursor: str | None, outcome: str,
        consume_channel: str | None = None,
        bootstrap_input_sha: str | None = None,
    ) -> None:
        """Record a run; a sent morning review consumes its existing once-only rows.

        Weekly packets pass ``kind='weekly'``, ``outcome='answered'`` and
        ``consume_channel='weekly'`` to consume only logged stale-request rows
        at this exact packet time/channel, atomically with the successful run.
        Failed/aborted packets consume nothing. No morning delivery is inferred.
        ``bootstrap_input_sha`` is consumed only by a successful review, atomically with its run.
        """
        if consume_channel is not None and (kind != "weekly" or consume_channel != "weekly"):
            raise ValueError("weekly consumption requires kind='weekly' and channel='weekly'")
        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO runs (kind, started_at, last_event_cursor, outcome) VALUES (?, ?, ?, ?)",
                    (kind, started_at.isoformat(), last_event_cursor, outcome),
                )
                if kind == "review" and outcome == "sent":
                    packet = self.morning_packet(today_in_singapore(started_at))
                    if packet is not None and (packet.prepared_at.isoformat() != started_at.isoformat()
                            or packet.bootstrap_input_sha != bootstrap_input_sha):
                        raise WhyLogError("a sent morning must consume the exact prepared packet and timestamp")
                    if bootstrap_input_sha is not None:
                        updated = self._connection.execute(
                            "UPDATE morning_bootstrap SET completed_at=? WHERE id=1 AND input_sha=? AND completed_at IS NULL",
                            (started_at.isoformat(), bootstrap_input_sha),
                        )
                        if updated.rowcount != 1:
                            raise WhyLogError("sent morning bootstrap must match the staged, unconsumed input SHA")
                    # Only actually logged, delivered lines consume a once-only request.
                    # The sent run and markers commit together; prune never deletes markers.
                    delivered = self._delivered_review_rows()
                    self._connection.executemany(
                        "INSERT OR IGNORE INTO request_review_consumption (item_ref, rule, surfaced_at) VALUES (?, ?, ?)",
                        ((row["item_ref"], row["rule"], row["surfaced_at"]) for row in delivered
                         if row["surfaced_at"] == started_at.isoformat() and row["rule"] in _REQUEST_ONCE_RULES),
                    )
                    if packet is not None:
                        self._connection.execute("UPDATE morning_packets SET completed_at=? WHERE day=?",
                                                 (started_at.isoformat(), today_in_singapore(started_at).isoformat()))
                elif kind == "weekly" and outcome == "answered" and consume_channel == "weekly":
                    self._connection.execute(
                        """INSERT OR IGNORE INTO request_review_consumption (item_ref, rule, surfaced_at)
                        SELECT item_ref, rule, surfaced_at FROM surfacings
                        WHERE channel=? AND surfaced_at=? AND rule='stale_request'""",
                        (consume_channel, started_at.isoformat()),
                    )
                self._retain_shown_refs()
        except sqlite3.Error as exc:
            raise WhyLogError("could not write a run row") from exc

    def signals_since(self, after_id: int) -> tuple[SignalRecord, ...]:
        """Signal rows newer than `after_id`, ascending - the moments poll's own feed."""

        rows = self._connection.execute(
            "SELECT * FROM signals WHERE id>? ORDER BY id ASC", (after_id,)
        ).fetchall()
        return tuple(
            SignalRecord(id=row["id"], kind=row["kind"], actor=row["actor"],
                         at=datetime.fromisoformat(row["at"]), detail=row["detail"])
            for row in rows
        )

    def latest_cursor(self, kind: str) -> str | None:
        """The most recent `last_event_cursor` recorded for a `runs.kind` (e.g. one
        moments-poll signal type), or `None` if that kind has never run."""

        row = self._connection.execute(
            "SELECT last_event_cursor FROM runs WHERE kind=? AND last_event_cursor IS NOT NULL ORDER BY id DESC LIMIT 1",
            (kind,),
        ).fetchone()
        return row["last_event_cursor"] if row else None

    def note_placement_notice(self, *, day: date, key: str, title: str, noted_at: datetime) -> bool:
        """Step I §5a: a task that no longer fits today's plan, kept until a daytime return carries
        it; at most once per task per day. True when it's new."""

        try:
            with self._connection:
                cursor = self._connection.execute(
                    "INSERT OR IGNORE INTO placement_notices (day, key, title, noted_at) VALUES (?, ?, ?, ?)",
                    (day.isoformat(), key, title, noted_at.isoformat()),
                )
                return cursor.rowcount == 1
        except sqlite3.Error as exc:
            raise WhyLogError("could not note a placement notice") from exc

    def pending_placement_notices(self, day: date) -> tuple[dict[str, str], ...]:
        rows = self._connection.execute(
            "SELECT key, title FROM placement_notices WHERE day=? AND sent_at IS NULL ORDER BY noted_at, key",
            (day.isoformat(),),
        ).fetchall()
        return tuple({"key": row["key"], "title": row["title"]} for row in rows)

    def mark_placement_notices_sent(self, day: date, keys: Iterable[str], sent_at: datetime) -> None:
        try:
            with self._connection:
                self._connection.executemany(
                    "UPDATE placement_notices SET sent_at=? WHERE day=? AND key=?",
                    [(sent_at.isoformat(), day.isoformat(), key) for key in keys],
                )
        except sqlite3.Error as exc:
            raise WhyLogError("could not mark placement notices sent") from exc

    def sent_today(self, kind: str, today: date) -> bool:
        """Whether a `kind` run already sent something today (Singapore date; replay-safety)."""

        rows = self._connection.execute(
            "SELECT started_at FROM runs WHERE kind=? AND outcome LIKE 'sent%'", (kind,)
        ).fetchall()
        return any(today_in_singapore(datetime.fromisoformat(row["started_at"])) == today for row in rows)

    def signals_on(self, kind: str, day: date) -> tuple[SignalRecord, ...]:
        """One kind's signal rows on one Singapore day, oldest first (step I: day_settled, day_change)."""

        rows = self._connection.execute("SELECT * FROM signals WHERE kind=? ORDER BY id ASC", (kind,)).fetchall()
        records = (
            SignalRecord(id=row["id"], kind=row["kind"], actor=row["actor"],
                         at=datetime.fromisoformat(row["at"]), detail=row["detail"])
            for row in rows
        )
        return tuple(record for record in records if today_in_singapore(record.at) == day)

    def record_placement(
        self, *, day: date, ran_at: datetime, fingerprint: str, changes_seen: int, plan: Mapping[str, Any],
        outcome: str, error: str | None = None,
    ) -> None:
        """One placement run (step I; build plan slice 4): the day's plan as it stood, and what happened."""

        try:
            with self._connection:
                self._connection.execute(
                    "INSERT INTO placement_runs (day, ran_at, fingerprint, changes_seen, plan, outcome, error)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (day.isoformat(), ran_at.isoformat(), fingerprint, changes_seen, json.dumps(plan, sort_keys=True),
                     outcome, error),
                )
        except sqlite3.Error as exc:
            raise WhyLogError("could not write a placement run row") from exc

    @contextmanager
    def schedule_return_lock(self):
        """Nonblocking OS lock; a stopped process cannot leave a stale lease.

        This coordinates the existing why-log's read/send/consume sequence, without
        holding a SQLite transaction while delivery happens. The file holds no data.
        """
        if not self._schedule_guard.acquire(blocking=False):
            yield False
            return
        try:
            if str(self._path) == ":memory:":
                yield True
                return
            with self._path.with_name(self._path.name + ".schedule.lock").open("a+b") as handle:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    if os.fstat(handle.fileno()).st_size == 0:
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                else:
                    import fcntl
                try:
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    yield False
                    return
                try:
                    yield True
                finally:
                    if os.name == "nt":
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._schedule_guard.release()

    def take_placement_lock(self, owner: str, now: datetime, *, stale_after: timedelta) -> bool:
        """One placement run at a time, across processes sharing this file (an immediate write
        transaction decides): True when ``owner`` now holds it; a holder older than ``stale_after``
        is taken over."""

        try:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute("SELECT owner, taken_at FROM placement_lock WHERE id=1").fetchone()
                held = row is not None and row["owner"] is not None
                if held and now - datetime.fromisoformat(row["taken_at"]) < stale_after:
                    self._connection.execute("COMMIT")
                    return False
                self._connection.execute(
                    "INSERT INTO placement_lock (id, owner, taken_at) VALUES (1, ?, ?)"
                    " ON CONFLICT(id) DO UPDATE SET owner=excluded.owner, taken_at=excluded.taken_at",
                    (owner, now.isoformat()),
                )
                self._connection.execute("COMMIT")
                return True
            except BaseException:
                self._connection.execute("ROLLBACK")
                raise
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc) or "busy" in str(exc):
                return False
            raise WhyLogError("could not take the placement lock") from exc

    def release_placement_lock(self, owner: str) -> None:
        try:
            with self._connection:
                self._connection.execute("UPDATE placement_lock SET owner=NULL, taken_at=NULL WHERE id=1 AND owner=?", (owner,))
        except sqlite3.Error as exc:
            raise WhyLogError("could not release the placement lock") from exc

    def latest_placement(self, day: date | None = None) -> dict[str, Any] | None:
        """The newest placement run, for one day or any: its columns, with ``plan`` parsed."""

        query, parameters = "SELECT * FROM placement_runs", ()
        if day is not None:
            query, parameters = query + " WHERE day=?", (day.isoformat(),)
        row = self._connection.execute(query + " ORDER BY id DESC LIMIT 1", parameters).fetchone()
        if row is None:
            return None
        found = dict(row)
        found["plan"] = json.loads(found["plan"])
        return found

    def read_view(self, day):
        """Capture card read facts in one short transaction, released before writes.

        Question reconciliation and surfacing remain live operations on this log;
        only the immutable input history/packet/settlement facts are shared.
        """
        connection = self._connection
        connection.execute("SAVEPOINT review_card_read")
        try:
            rows = connection.execute("SELECT * FROM surfacings ORDER BY surfaced_at, id").fetchall()
            settlements = connection.execute("SELECT at,detail FROM signals WHERE kind='day_settled' AND actor='system:review-card' ORDER BY id").fetchall()
            general = self._current_card_picks(tuple(_row_to_history(row) for row in rows), settlements=settlements)
            delivered = {row["id"] for row in self._delivered_review_rows()}
            morning = tuple(_row_to_history(row) for row in rows
                if not (row["channel"] == "review" and row["rule"] in _REQUEST_ONCE_RULES)
                and not (row["channel"] == "weekly" and row["rule"] == "stale_request")
                and (row["channel"] != "review" or row["id"] in delivered))
            consumed = connection.execute(
                "SELECT item_ref, rule, surfaced_at FROM request_review_consumption ORDER BY surfaced_at, item_ref, rule"
            ).fetchall()
            markers = tuple(HistoryRow(item_ref=row["item_ref"], channel="review", rule=row["rule"],
                record_sha256="", record_state=None, surfaced_at=datetime.fromisoformat(row["surfaced_at"]))
                for row in consumed if row["rule"] in _REQUEST_ONCE_RULES)
            morning = self._current_card_picks(morning + markers, settlements=settlements)
            packet, bootstrap, shown = self.morning_packet(day), self.morning_bootstrap(), self.shown_item_refs()
            signals = {day + timedelta(days=offset): self.signals_on("day_settled", day + timedelta(days=offset))
                       for offset in (-2, -1, 0)}
            return ReviewLogReadView(self, general, morning, day, packet, bootstrap, shown, signals)
        finally:
            connection.execute("RELEASE SAVEPOINT review_card_read")

    def history(self) -> tuple[HistoryRow, ...]:
        rows = self._connection.execute("SELECT * FROM surfacings ORDER BY surfaced_at ASC, id ASC").fetchall()
        return self._current_card_picks(tuple(_row_to_history(row) for row in rows))

    def _current_card_picks(self, history, *, settlements=None):
        """Effective picks for readers; the original audit rows remain unchanged.

        Only explicit card selections supersede older picks for the same day and
        shown item. A later answer from another surface still wins normally.
        """
        selections, card_days = {}, {}
        if settlements is None:
            settlements = self._connection.execute("SELECT at,detail FROM signals WHERE kind='day_settled' AND actor='system:review-card' ORDER BY id")
        for row in settlements:
            try:
                value = json.loads(row["detail"] or "null")
            except (ValueError, TypeError):
                continue
            if not isinstance(value, dict) or value.get("format") != "review-card-picks/v1":
                continue
            at = datetime.fromisoformat(row["at"])
            day = date.fromisoformat(value["review_day"]) if value.get("review_day") else (at.astimezone(SINGAPORE) - timedelta(hours=6)).date()
            for ref in value["eligible"]:
                selections[(day, ref)] = (at, ref in value["picks"])
                card_days[(at, ref)] = day
        result = []
        for row in history:
            at = row.responded_at or row.surfaced_at
            day = (card_days.get((at, row.item_ref), (at.astimezone(SINGAPORE) - timedelta(hours=6)).date())
                   if row.channel == "review_card" and row.rule == "card_pick" else today_in_singapore(at))
            selected = selections.get((day, row.item_ref))
            if row.response == "picked_today" and selected and at <= selected[0] and not selected[1]:
                row = replace(row, response="not_now")
            result.append(row)
        return tuple(result)

    def set_review_picks(self, picks, eligible, *, now, operation_id, review_day=None):
        """Freeze this yes atomically; exact retries cannot restore older choices."""
        picks, eligible = tuple(dict.fromkeys(picks)), tuple(dict.fromkeys(eligible))
        day = review_day or (now.astimezone(SINGAPORE) - timedelta(hours=6)).date()
        if not isinstance(day, date) or isinstance(day, datetime):
            raise WhyLogError("review settlement needs its explicit review day")
        if not set(picks) <= set(eligible) or any(not isinstance(ref, str) or not ref for ref in eligible):
            raise WhyLogError("review picks must name only currently offered things")
        detail = json.dumps(dict(format="review-card-picks/v1", operation_id=operation_id,
                                 picks=list(picks), eligible=list(eligible), review_day=day.isoformat()), sort_keys=True)
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                previous = self._connection.execute("SELECT detail FROM signals WHERE kind='day_settled' AND actor='system:review-card'").fetchall()
                for row in previous:
                    try:
                        value = json.loads(row["detail"] or "null")
                    except (ValueError, TypeError):
                        continue
                    if isinstance(value, dict) and value.get("operation_id") == operation_id:
                        # Older card journals used this same frozen operation
                        # shape before making the six-am day explicit.
                        if "review_day" not in value:
                            value["review_day"] = day.isoformat()
                        if value != json.loads(detail):
                            raise WhyLogError("review settlement retry differs from its frozen picks")
                        self._connection.commit()
                        return
                for ref in eligible:
                    source = self._connection.execute("SELECT * FROM surfacings WHERE item_ref=? ORDER BY surfaced_at DESC,id DESC LIMIT 1", (ref,)).fetchone()
                    if source is None:
                        raise WhyLogError("a selected thing has not been shown in the review")
                    self._connection.execute("""INSERT INTO surfacings
                        (surfaced_at,channel,signal,item_ref,rule,record_sha256,record_path,record_state,
                         fields_used,assumptions,unknowns,response,response_words,responded_at)
                        VALUES (?,'review_card','owner_tap',?,'card_pick',?,?,?,?,?,?,?,'Yes, that''s my day',?)""",
                        (now.isoformat(), ref, source["record_sha256"], source["record_path"], source["record_state"],
                         json.dumps({"operation_id": operation_id, "review_day": day.isoformat()}), "[]", "[]",
                         "picked_today" if ref in picks else "not_now", now.isoformat()))
                self._connection.execute("INSERT INTO signals(kind,actor,at,detail) VALUES ('day_settled','system:review-card',?,?)", (now.isoformat(), detail))
                self._connection.commit()
            except BaseException:
                self._connection.rollback()
                raise

    def request_review_history(self) -> tuple[HistoryRow, ...]:
        """Review history with non-expiring W4 consumption, excluding failed-send attempts.

        Keep full reasons on the existing 90-day retention. These minimal markers
        carry neither proof nor request records and are only for the once-only rules.
        """
        history = tuple(row for row in self.history()
                        if not (row.channel == "review" and row.rule in _REQUEST_ONCE_RULES)
                        and not (row.channel == "weekly" and row.rule == "stale_request"))
        rows = self._connection.execute(
            "SELECT item_ref, rule, surfaced_at FROM request_review_consumption ORDER BY surfaced_at, item_ref, rule"
        ).fetchall()
        return history + tuple(
            HistoryRow(item_ref=row["item_ref"], channel="review", rule=row["rule"],
                       record_sha256="", record_state=None,
                       surfaced_at=datetime.fromisoformat(row["surfaced_at"]))
            for row in rows
        )

    def full_reason(self, item_ref: str) -> tuple[FullReason, ...]:
        """Every stored column for every surfacing of one item ("What's the full reason for 3?")."""

        rows = self._connection.execute(
            "SELECT * FROM surfacings WHERE item_ref=? ORDER BY surfaced_at ASC, id ASC", (item_ref,)
        ).fetchall()
        return tuple(_row_to_full_reason(row) for row in rows)

    def prune(self, now: datetime) -> int:
        """Remove surfacing and placement-run rows older than 90 days; nothing else deletes from the
        log. Returns how many rows went, of both kinds."""

        cutoff = (now - timedelta(days=RETENTION_DAYS)).isoformat()
        try:
            with self._connection:
                self._retain_shown_refs()
                cursor = self._connection.execute("DELETE FROM surfacings WHERE surfaced_at < ?", (cutoff,))
                runs = self._connection.execute("DELETE FROM placement_runs WHERE ran_at < ?", (cutoff,))
                # Full delivered packet text follows the existing reason retention;
                # unfinished packets stay available for exact delivery recovery.
                self._connection.execute(
                    "DELETE FROM morning_packets WHERE prepared_at < ? AND completed_at IS NOT NULL", (cutoff,))
                self._connection.execute("DELETE FROM name_requests WHERE prepared_at < ?", (cutoff,))
                return cursor.rowcount + runs.rowcount
        except sqlite3.Error as exc:
            raise WhyLogError("could not prune the why-log") from exc


def _locked(method):
    @functools.wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)

    return locked


for _name, _method in list(vars(WhyLog).items()):
    if callable(_method) and not _name.startswith("__"):
        setattr(WhyLog, _name, _locked(_method))
del _name, _method


@dataclass(frozen=True)
class ReviewLogReadView:
    """Interaction-only read facts; all reconciliation and writes use the live log."""
    log: WhyLog
    general: tuple
    morning: tuple
    day: date
    packet: object
    bootstrap: object
    shown: frozenset
    signals: dict

    def history(self):
        return self.general

    def morning_history(self):
        return self.morning

    def morning_packet(self, day):
        return self.packet if day == self.day else self.log.morning_packet(day)

    def morning_bootstrap(self):
        return self.bootstrap

    def shown_item_refs(self):
        return self.shown

    def signals_on(self, kind, day):
        return self.signals[day] if kind == "day_settled" and day in self.signals else self.log.signals_on(kind, day)

    def __getattr__(self, name):
        return getattr(self.log, name)
