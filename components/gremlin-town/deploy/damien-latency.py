#!/usr/bin/env python3
"""Turn Diablo's agent.log (and optionally the hub's usage log) into a
per-turn latency report, so every re-test of Diablo's speed is measured the
same way.

Stdlib only. This is meant to run on the hub host's system ``python3`` (3.12),
which has no installed packages, so it never imports ``gremlin_town`` or any
third-party library.

Privacy, hard rule: this script never reads, stores or prints message text
(the ``msg='...'`` field of an ``inbound message`` log line) or any Brain
record content. The inbound-message pattern stops matching before ``msg=``,
so that text is never even captured, let alone printed. Only metadata is
used: timestamps, token counts, latencies, outcomes and error codes.

Log sources
-----------
On the live host, ``agent.log`` is the superset: it holds the gateway's own
``inbound message``/``response ready`` lines *and* the per-model-call
``agent.conversation_loop`` lines. ``gateway.log`` holds only the former (no
model calls at all) and its lines are byte-identical copies of the ones in
``agent.log``. Point ``--log`` at ``agent.log`` (plus ``agent.log.1`` etc. for
rotations) — that alone is enough. Passing ``gateway.log`` too is harmless but
unnecessary: any line seen before (same timestamp, level, session, logger and
message) across ``--log`` paths is deduplicated, so passing overlapping files
never double-counts a turn.

Turn model
----------
A "turn" is one round trip through the gateway: an ``inbound message`` line
(logger ``gateway.run``) opens it, and the matching ``response ready`` line
for the same ``chat`` closes it. In between, ``agent.conversation_loop``
lines tagged ``[session]`` record each model call and mark when the model
finished. The bracketed session tag is the only thing used to attribute a
model call to a turn; the ``session=`` field written inside a ``Turn ended``
message is informational only and is not assumed to equal the bracket tag
(the two are not always the same string).

``--since``/``--until`` are applied to raw events before they are associated
into turns, and events outside that window are dropped silently: a model
call from days before ``--since`` is never even considered for attribution,
so it cannot generate an "outside any open turn" note. A note only ever
covers something that happened inside the requested window.

Usage
-----
    python3 damien-latency.py --log agent.log [--log agent.log.1 ...] \\
        [--usage-db brain-usage.sqlite3 | --usage-json calls.json] [--actor hermes-wa] \\
        [--since 2026-09-26] [--until 2026-09-27] [--json]

``--log`` may be repeated for rotated files in any order: every line from
every file is timestamp-sorted (and deduplicated) before turns are
reassembled. ``--usage-json`` is an alternative to ``--usage-db`` for when the
caller cannot open the usage database directly (see "Measuring Diablo's
turns" in ``components/gremlin-town/docs/operations.md`` for the two-account
read that produces it, and for the exact read-only commands used against the
live hub).
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import statistics
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

SGT = timezone(timedelta(hours=8))  # Singapore Standard Time: fixed UTC+8, no DST.
DEFAULT_ACTOR = "hermes-wa"  # Diablo's actor in the hub's usage log.
CACHE_MISS_MAX_PCT = 50  # a call under this cache-hit percentage counts as a miss.

_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) (?P<level>\S+) "
    r"(?:\[(?P<session>[^\]]+)\] )?(?P<logger>[A-Za-z0-9_.]+): (?P<rest>.*)$"
)
# Stops right before msg=: the message body is never captured.
_INBOUND = re.compile(r"^inbound message: platform=(?P<platform>\S+) user=.*? chat=(?P<chat>\S+) msg=")
_API_CALL = re.compile(
    r"^API call #(?P<num>\d+): model=(?P<model>\S+) provider=(?P<provider>\S+) "
    r"in=(?P<in>\d+) out=(?P<out>\d+) total=(?P<total>\d+) latency=(?P<latency>[\d.]+)s "
    r"cache=(?P<cache_hit>\d+)/(?P<cache_total>\d+) \((?P<cache_pct>\d+)%\)\s*$"
)
_TURN_ENDED = re.compile(
    r"^Turn ended: reason=(?P<reason>.+?) model=(?P<model>\S+) "
    r"api_calls=(?P<api_calls>\d+)/(?P<api_calls_max>\d+) budget=(?P<budget>\d+)/(?P<budget_max>\d+) "
    r"tool_turns=(?P<tool_turns>\d+) last_msg_role=(?P<role>\S+) response_len=(?P<response_len>\d+) "
    r"session=(?P<reported_session>\S+)\s*$"
)
_RESPONSE_READY = re.compile(
    r"^response ready: platform=(?P<platform>\S+) chat=(?P<chat>\S+) time=(?P<time>[\d.]+)s "
    r"api_calls=(?P<api_calls>\d+) response=(?P<response_len>\d+) chars\s*$"
)


def _parse_ts(raw: str) -> datetime:
    return datetime.strptime(raw.replace(",", ".", 1), "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=UTC)


class _Inbound(NamedTuple):
    ts: datetime
    chat: str


class _ApiCall(NamedTuple):
    ts: datetime
    session: str | None
    call_number: int
    model: str
    output_tokens: int
    latency_s: float
    cache_pct: int


class _TurnEnded(NamedTuple):
    ts: datetime
    session: str | None


class _ResponseReady(NamedTuple):
    ts: datetime
    chat: str
    wait_s: float


_Event = _Inbound | _ApiCall | _TurnEnded | _ResponseReady


def _match_line(line: str):
    """Return the ``_LINE`` match for a recognized line, or ``None``."""

    return _LINE.match(line.rstrip("\r\n"))


def _event_from_match(ts: datetime, match: re.Match[str]) -> _Event | None:
    """Turn one already-timestamped, already-matched line into a typed event."""

    rest = match["rest"]
    session = match["session"]
    inbound = _INBOUND.match(rest)
    if inbound is not None:
        return _Inbound(ts, inbound["chat"])
    call = _API_CALL.match(rest)
    if call is not None:
        return _ApiCall(
            ts, session, int(call["num"]), call["model"], int(call["out"]),
            float(call["latency"]), int(call["cache_pct"]),
        )
    if _TURN_ENDED.match(rest) is not None:
        return _TurnEnded(ts, session)
    ready = _RESPONSE_READY.match(rest)
    if ready is not None:
        return _ResponseReady(ts, ready["chat"], float(ready["time"]))
    return None


def parse_log_lines(lines: Iterable[str]) -> list[_Event]:
    """Parse the four known line kinds from a single stream; everything else is
    ignored. Does not deduplicate or filter by time; see ``read_log_events``,
    which does both and is what merges more than one file.
    """

    events: list[_Event] = []
    for line in lines:
        match = _match_line(line)
        if match is None:
            continue
        event = _event_from_match(_parse_ts(match["ts"]), match)
        if event is not None:
            events.append(event)
    return events


def read_log_events(
    paths: Iterable[Path], *, since: datetime | None = None, until: datetime | None = None,
) -> list[_Event]:
    """Read every path into one timestamp-sorted, deduplicated event stream.

    Rotated files (``agent.log.1`` and older) may be passed in any order:
    lines are merged by their own timestamp, not by file order. If the exact
    same line (same timestamp, level, session, logger and message) appears in
    more than one path — e.g. ``agent.log``'s ``gateway.run`` lines are
    byte-identical copies of ``gateway.log``'s — only the first occurrence is
    kept, so passing overlapping files never double-counts a turn.

    ``since``/``until``, when given, are applied here, before any event is
    associated into a turn: an event outside the window is dropped as if it
    were never in the file, so it can never generate an "outside any open
    turn" note in ``build_turns``. A note there only ever covers something
    that happened inside the requested window.
    """

    seen: set[tuple[str, str, str | None, str, str]] = set()
    events: list[_Event] = []
    for path in paths:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = _match_line(line)
                if match is None:
                    continue
                ts = _parse_ts(match["ts"])
                if since is not None and ts < since:
                    continue
                if until is not None and ts > until:
                    continue
                key = (match["ts"], match["level"], match["session"], match["logger"], match["rest"])
                if key in seen:
                    continue
                seen.add(key)
                event = _event_from_match(ts, match)
                if event is not None:
                    events.append(event)
    events.sort(key=lambda event: event.ts)
    return events


@dataclass(frozen=True, slots=True)
class ModelCall:
    call_number: int
    model: str
    output_tokens: int
    latency_s: float
    cache_pct: int

    @property
    def cache_miss(self) -> bool:
        return self.cache_pct < CACHE_MISS_MAX_PCT


@dataclass(frozen=True, slots=True)
class BrainWindow:
    calls: int
    total_ms: int
    refusal_codes: tuple[str, ...]


@dataclass(slots=True)
class Turn:
    start: datetime
    end: datetime | None
    wait_s: float | None
    model_calls: list[ModelCall] = field(default_factory=list)
    brain: BrainWindow | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return self.end is not None and self.wait_s is not None

    @property
    def model_time_s(self) -> float:
        return sum(call.latency_s for call in self.model_calls)

    @property
    def brain_time_s(self) -> float | None:
        return self.brain.total_ms / 1000 if self.brain is not None else None


@dataclass(slots=True)
class _PendingTurn:
    start: datetime
    model_calls: list[ModelCall] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def finish(self, end: datetime, wait_s: float) -> Turn:
        return Turn(self.start, end, wait_s, self.model_calls, warnings=list(self.warnings))

    def unfinished(self) -> Turn:
        turn = Turn(self.start, None, None, self.model_calls, warnings=list(self.warnings))
        turn.warnings.append("no matching response ready in the log window; turn is incomplete")
        return turn


def build_turns(events: Iterable[_Event]) -> tuple[list[Turn], list[str]]:
    """Reassemble turns from a time-ordered event stream.

    Returns ``(turns, warnings)``. A turn is opened by ``inbound message`` and
    closed by the matching ``response ready`` for the same ``chat``. A model
    call or ``Turn ended`` line carries a bracketed session tag instead of a
    chat; the chat that tag belongs to is learned the first time it is seen
    while exactly one turn is open, then remembered for later turns on the
    same session. This never crashes on an ambiguous or orphaned line: it
    notes the ambiguity and keeps going, so a caller always gets a report
    plus a visible list of anything it could not attribute with confidence.
    """

    open_by_chat: dict[str, _PendingTurn] = {}
    session_chat: dict[str, str] = {}
    turns: list[Turn] = []
    warnings: list[str] = []

    def target_for(session: str | None) -> _PendingTurn | None:
        if session is not None and session in session_chat:
            pending = open_by_chat.get(session_chat[session])
            if pending is not None:
                return pending
        if len(open_by_chat) == 1:
            chat, pending = next(iter(open_by_chat.items()))
            if session is not None:
                session_chat[session] = chat
            return pending
        if len(open_by_chat) > 1:
            chat, pending = min(open_by_chat.items(), key=lambda item: item[1].start)
            pending.warnings.append(f"ambiguous session attribution while {len(open_by_chat)} turns were open")
            return pending
        return None

    for event in events:
        if isinstance(event, _Inbound):
            stale = open_by_chat.pop(event.chat, None)
            if stale is not None:
                turns.append(stale.unfinished())
            open_by_chat[event.chat] = _PendingTurn(start=event.ts)
        elif isinstance(event, _ApiCall):
            pending = target_for(event.session)
            if pending is None:
                warnings.append(f"model call #{event.call_number} at {event.ts.isoformat()} was outside any open turn; ignored")
                continue
            pending.model_calls.append(
                ModelCall(event.call_number, event.model, event.output_tokens, event.latency_s, event.cache_pct)
            )
        elif isinstance(event, _TurnEnded):
            target_for(event.session)  # only learns the session -> chat mapping; nothing else to record.
        elif isinstance(event, _ResponseReady):
            pending = open_by_chat.pop(event.chat, None)
            if pending is None:
                warnings.append(f"response ready at {event.ts.isoformat()} had no matching inbound message; ignored")
                continue
            turns.append(pending.finish(event.ts, event.wait_s))

    for pending in open_by_chat.values():
        turns.append(pending.unfinished())
    turns.sort(key=lambda turn: turn.start)
    return turns, warnings


def read_brain_calls(db_path: Path, actor: str | None) -> list[tuple[datetime, str, str | None, int]]:
    """Read ``(at, outcome, error_code, duration_ms)`` rows, oldest first.

    Opened with plain ``mode=ro`` (no ``immutable=1``), the same convention as
    ``gremlin_town.mcp.brain_usage.read_usage`` and every other live read of
    this file documented in this vault: the database is actively written
    under WAL by the running service, so ``immutable=1`` (for a static,
    already-extracted snapshot) would risk a stale read here instead of a
    safe one. Never touches ``record_id``/``operation_id`` or any argument:
    those columns exist in the table but are not record content either
    (metadata only, per ``brain_usage.py``), and this tool does not need them.
    """

    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        query = "SELECT at, outcome, error_code, duration_ms FROM brain_usage"
        params: tuple[str, ...] = ()
        if actor is not None:
            query += " WHERE actor = ?"
            params = (actor,)
        rows = connection.execute(query, params).fetchall()
    finally:
        connection.close()
    parsed = [
        (datetime.fromisoformat(at).astimezone(UTC), outcome, error_code, int(duration_ms))
        for at, outcome, error_code, duration_ms in rows
    ]
    parsed.sort(key=lambda row: row[0])
    return parsed


def load_brain_calls_json(text: str) -> list[tuple[datetime, str, str | None, int]]:
    """Parse a ``--usage-json`` export into the same shape ``read_brain_calls``
    returns: ``(at, outcome, error_code, duration_ms)``, oldest first.

    The export is a JSON array of objects with ``at`` (ISO 8601, any offset),
    ``outcome``, ``duration_ms`` and optionally ``error_code``. It is produced
    by a separate, simpler read as the usage database's own owner (see
    "Measuring Damien's turns" in operations.md); this function never opens
    the database itself, and does not re-filter by actor — an export should
    already be scoped to the actor wanted.
    """

    try:
        rows = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid --usage-json content: {exc}") from exc
    if not isinstance(rows, list):
        raise ValueError("--usage-json must be a JSON array of usage rows")
    parsed = []
    for row in rows:
        if not isinstance(row, dict) or not {"at", "outcome", "duration_ms"} <= row.keys():
            raise ValueError("--usage-json row is missing at/outcome/duration_ms")
        at = datetime.fromisoformat(row["at"])
        if at.tzinfo is None:
            raise ValueError(f"--usage-json row 'at' has no offset: {row['at']!r}")
        parsed.append((at.astimezone(UTC), str(row["outcome"]), row.get("error_code"), int(row["duration_ms"])))
    parsed.sort(key=lambda row: row[0])
    return parsed


def brain_window(
    calls: list[tuple[datetime, str, str | None, int]], start: datetime, end: datetime,
) -> BrainWindow:
    in_window = [call for call in calls if start <= call[0] <= end]
    total_ms = sum(call[3] for call in in_window)
    refusals = tuple(sorted({call[2] for call in in_window if call[1] == "error" and call[2]}))
    return BrainWindow(len(in_window), total_ms, refusals)


def summarize(turns: list[Turn], *, brain_measured: bool) -> dict[str, object]:
    complete = [turn for turn in turns if turn.complete]
    waits = [turn.wait_s for turn in complete]
    call_counts = [len(turn.model_calls) for turn in complete]
    total_wait = sum(waits)
    total_model_time = sum(turn.model_time_s for turn in complete)
    all_calls = [call for turn in turns for call in turn.model_calls]
    return {
        "turns_total": len(turns),
        "turns_complete": len(complete),
        "turns_incomplete": len(turns) - len(complete),
        "median_wait_s": statistics.median(waits) if waits else None,
        "model_calls_per_turn_mean": statistics.mean(call_counts) if call_counts else None,
        "model_calls_per_turn_min": min(call_counts) if call_counts else None,
        "model_calls_per_turn_max": max(call_counts) if call_counts else None,
        "share_of_time_in_model_pct": (total_model_time / total_wait * 100) if total_wait else None,
        "cache_misses": sum(1 for call in all_calls if call.cache_miss),
        "cache_miss_threshold_pct": CACHE_MISS_MAX_PCT,
        "brain_measured": brain_measured,
    }


def render_text(turns: list[Turn], summary: dict[str, object], warnings: list[str]) -> str:
    lines: list[str] = []
    for index, turn in enumerate(turns, start=1):
        lines.append(f"Turn {index}")
        lines.append(
            f"  Start: {turn.start.strftime('%Y-%m-%d %H:%M:%S UTC')} "
            f"({turn.start.astimezone(SGT).strftime('%Y-%m-%d %H:%M:%S SGT')})"
        )
        if turn.complete:
            summary_bits = [
                f"Wait: {turn.wait_s:.1f}s",
                f"Model calls: {len(turn.model_calls)} ({turn.model_time_s:.1f}s)",
            ]
            if turn.brain is not None:
                summary_bits.append(f"Brain calls: {turn.brain.calls} ({turn.brain_time_s:.1f}s)")
            elif summary["brain_measured"]:
                summary_bits.append("Brain calls: 0 (0.0s)")
            lines.append("  " + " | ".join(summary_bits))
        else:
            lines.append("  Wait: incomplete (no response ready in the log window)")
        for call in turn.model_calls:
            flag = " CACHE MISS" if call.cache_miss else ""
            lines.append(
                f"    #{call.call_number} model={call.model} latency={call.latency_s:.2f}s "
                f"out={call.output_tokens} cache={call.cache_pct}%{flag}"
            )
        if turn.brain is not None:
            refusals = ", ".join(turn.brain.refusal_codes) if turn.brain.refusal_codes else "none"
            lines.append(f"  Brain: {turn.brain.calls} calls, {turn.brain.total_ms}ms total, refusals: {refusals}")
        for note in turn.warnings:
            lines.append(f"  Note: {note}")
        lines.append("")
    lines.append(
        f"Summary ({summary['turns_total']} turns: {summary['turns_complete']} complete, "
        f"{summary['turns_incomplete']} incomplete)"
    )
    if summary["median_wait_s"] is not None:
        lines.append(f"  Median wait: {summary['median_wait_s']:.1f}s")
        lines.append(
            f"  Model calls per turn: {summary['model_calls_per_turn_mean']:.1f} mean "
            f"(min {summary['model_calls_per_turn_min']}, max {summary['model_calls_per_turn_max']})"
        )
        share = summary["share_of_time_in_model_pct"]
        lines.append(f"  Share of time in model: {share:.1f}%" if share is not None else "  Share of time in model: n/a")
    else:
        lines.append("  No complete turns in range.")
    if not summary["brain_measured"]:
        lines.append("  Brain calls: not measured (no --usage-db or --usage-json)")
    lines.append(f"  Cache misses: {summary['cache_misses']} (calls under {summary['cache_miss_threshold_pct']}% cache hit)")
    for note in warnings:
        lines.append(f"Note: {note}")
    return "\n".join(lines) + "\n"


def to_json(turns: list[Turn], summary: dict[str, object], warnings: list[str]) -> dict[str, object]:
    return {
        "turns": [
            {
                "start_utc": turn.start.astimezone(UTC).isoformat(),
                "start_sgt": turn.start.astimezone(SGT).isoformat(),
                "end_utc": turn.end.astimezone(UTC).isoformat() if turn.end is not None else None,
                "wait_s": turn.wait_s,
                "complete": turn.complete,
                "model_calls": [
                    {
                        "call_number": call.call_number,
                        "model": call.model,
                        "latency_s": call.latency_s,
                        "output_tokens": call.output_tokens,
                        "cache_pct": call.cache_pct,
                        "cache_miss": call.cache_miss,
                    }
                    for call in turn.model_calls
                ],
                "model_time_s": turn.model_time_s,
                "brain": (
                    {
                        "calls": turn.brain.calls,
                        "total_ms": turn.brain.total_ms,
                        "refusal_codes": list(turn.brain.refusal_codes),
                    }
                    if turn.brain is not None else None
                ),
                "brain_time_s": turn.brain_time_s,
                "warnings": list(turn.warnings),
            }
            for turn in turns
        ],
        "summary": summary,
        "warnings": list(warnings),
    }


def _parse_bound(value: str, *, end_of_day: bool) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    if end_of_day and len(value) <= 10:  # a bare date, e.g. "2026-09-26": use its last instant.
        dt = dt + timedelta(days=1) - timedelta(microseconds=1)
    return dt.astimezone(UTC)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="damien-latency.py",
        description="Turn Diablo's agent.log into a per-turn latency report. Never prints message text.",
    )
    parser.add_argument("--log", dest="logs", action="append", required=True, type=Path,
                         help="agent.log path (the superset log; gateway.log is an optional, redundant "
                              "subset); repeat for rotated files (agent.log.1 ...), any order")
    parser.add_argument("--usage-db", dest="usage_db", type=Path, default=None,
                         help="brain-usage.sqlite3 path, opened read-only")
    parser.add_argument("--usage-json", dest="usage_json", default=None,
                         help="path to a --usage-json export (or '-' for stdin) instead of --usage-db, for "
                              "when the caller cannot open that database directly; already actor-filtered, "
                              "so --actor is not re-applied to it")
    parser.add_argument("--actor", default=DEFAULT_ACTOR,
                         help=f"usage-log actor to include when --usage-db is given (default: {DEFAULT_ACTOR})")
    parser.add_argument("--since", default=None,
                         help="ISO date/time (UTC if no offset); drops events before this instant "
                              "before turns are built")
    parser.add_argument("--until", default=None,
                         help="ISO date/time (UTC if no offset); drops events after this instant "
                              "before turns are built")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a text report")
    args = parser.parse_args(argv)

    try:
        if args.usage_db is not None and args.usage_json is not None:
            raise ValueError("--usage-db and --usage-json are mutually exclusive")
        for path in args.logs:
            if not path.is_file():
                raise FileNotFoundError(f"no such log file: {path}")

        since = _parse_bound(args.since, end_of_day=False) if args.since else None
        until = _parse_bound(args.until, end_of_day=True) if args.until else None
        turns, warnings = build_turns(read_log_events(args.logs, since=since, until=until))

        brain_measured = args.usage_db is not None or args.usage_json is not None
        calls: list[tuple[datetime, str, str | None, int]] | None = None
        if args.usage_db is not None:
            if not args.usage_db.is_file():
                raise FileNotFoundError(f"no such usage database: {args.usage_db}")
            calls = read_brain_calls(args.usage_db, args.actor)
        elif args.usage_json is not None:
            text = sys.stdin.read() if args.usage_json == "-" else Path(args.usage_json).read_text(encoding="utf-8")
            calls = load_brain_calls_json(text)
        if calls is not None:
            for turn in turns:
                if turn.complete:
                    turn.brain = brain_window(calls, turn.start, turn.end)

        summary = summarize(turns, brain_measured=brain_measured)
        if args.json:
            print(json.dumps(to_json(turns, summary, warnings), indent=2))
        else:
            sys.stdout.write(render_text(turns, summary, warnings))
        return 0
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        print(f"damien-latency: refused ({type(exc).__name__}: {exc})", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
