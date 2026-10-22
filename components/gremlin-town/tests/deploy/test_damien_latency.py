"""Tests for ``deploy/damien-latency.py``, loaded the same way the repository's
other hyphenated deploy scripts are tested (see ``tests/recovery/test_oauth_restore.py``
for ``validate-backup.py``): the script is stdlib-only and meant to run on the
hub's bare system Python, so it is never made an importable package module.

Fixtures are entirely synthetic: made-up chat text and made-up Brain rows, in the
real ``agent.log``/``gateway.log``/``brain_usage`` line and schema shapes,
reproducing the shape of ``ops/audits/2026-09-26-damien-latency.md`` (four turns,
one cache miss, at least one Brain refusal) without repeating any real content.

On the live host ``agent.log`` is the superset (inbound/response-ready *and*
model-call/turn-ended lines); ``gateway.log`` carries only the former, as
byte-identical copies. Fixtures below default to writing the full combined
content to a file named ``agent.log``, and a couple of tests build a
``gateway.log``-shaped subset explicitly to exercise the overlap.
"""

from __future__ import annotations

import importlib.util
import json
import statistics
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from gremlin_town.mcp.brain_usage import BrainUsageEvent, BrainUsageLog

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "damien-latency.py"
spec = importlib.util.spec_from_file_location("damien_latency", SCRIPT)
damien_latency = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = damien_latency  # dataclass(slots=True) needs the module registered first.
spec.loader.exec_module(damien_latency)


SESSION = "20260921_060900_09c1a924"
CHAT = "75280899"
BASE = datetime(2026, 9, 26, 13, 42, 10, tzinfo=UTC)
TURN_STARTS = (0, 220, 460, 530)  # seconds after BASE; each turn ends well before the next starts.

# (msg, model calls: [(offset_s, latency_s, output_tokens, cache_pct), ...], wait_s,
#  brain calls: [(offset_s, outcome, error_code, duration_ms), ...])
TURN_SPECS = [
    (
        "TESTMSG_ONE what tasks are open",
        [(2, 2.1, 120, 99), (5, 1.9, 98, 100), (8, 2.3, 110, 99), (11, 1.9, 90, 100)],
        44.2,
        [(1, "ok", None, 400), (2, "ok", None, 600), (3, "ok", None, 500), (4, "ok", None, 500)],
    ),
    (
        "TESTMSG_TWO I need to study for the exam",
        [(2, 18.6, 665, 1), (21, 3.0, 140, 98), (25, 5.9, 300, 97), (32, 2.7, 78, 99), (42, 9.9, 568, 97)],
        51.0,
        [(1, "error", "brain.time.unresolved", 313), (3, "ok", None, 2490), (40, "ok", None, 2736), (50, "ok", None, 351)],
    ),
    (
        "TESTMSG_THREE call with Tommy later",
        [(2, 2.0, 90, 99), (6, 2.2, 100, 100), (10, 1.8, 80, 99)],
        28.0,
        [(1, "error", "brain.time.unresolved", 310), (5, "ok", None, 2362)],
    ),
    (
        "TESTMSG_FOUR I must eat dinner",
        [(2, 1.9, 70, 100), (5, 1.7, 60, 99)],
        11.6,
        [(2, "ok", None, 2300)],
    ),
]
ALL_MESSAGES = [msg for msg, _calls, _wait, _brain in TURN_SPECS]


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]


def _inbound(dt: datetime, msg: str) -> str:
    return (
        f"{_ts(dt)} INFO gateway.run: inbound message: platform=telegram user=Nic W "
        f"chat={CHAT} msg='{msg}' reply_to_id=None"
    )


def _api_call(dt: datetime, num: int, latency_s: float, out_tokens: int, cache_pct: int, session: str = SESSION) -> str:
    in_tokens = 168789
    return (
        f"{_ts(dt)} INFO [{session}] agent.conversation_loop: API call #{num}: model=grok-4.7 "
        f"provider=xai-oauth in={in_tokens} out={out_tokens} total={in_tokens + out_tokens} "
        f"latency={latency_s}s cache={cache_pct}/100 ({cache_pct}%)"
    )


def _gateway_only_lines(lines: list[str]) -> list[str]:
    """The subset of lines ``gateway.log`` carries on the live host: only the
    ``gateway.run`` (inbound/response-ready) lines, byte-identical to the
    copies inside ``agent.log`` -- no ``agent.conversation_loop`` lines."""

    return [line for line in lines if "gateway.run:" in line]


def _turn_ended(dt: datetime, api_calls: int) -> str:
    return (
        f"{_ts(dt)} INFO [{SESSION}] agent.conversation_loop: Turn ended: "
        f"reason=text_response(finish_reason=stop) model=grok-4.7 api_calls={api_calls}/500 "
        f"budget={api_calls}/500 tool_turns={api_calls * 20} last_msg_role=assistant "
        f"response_len=351 session=20260921_060900_09"
    )


def _response_ready(dt: datetime, wait_s: float, api_calls: int) -> str:
    return (
        f"{_ts(dt)} INFO gateway.run: response ready: platform=telegram chat={CHAT} "
        f"time={wait_s}s api_calls={api_calls} response=351 chars"
    )


def _build_log_lines(turn_starts=TURN_STARTS, specs=TURN_SPECS):
    lines: list[str] = []
    turns_meta = []
    for start_offset, (msg, calls, wait_s, brain) in zip(turn_starts, specs):
        start = BASE + timedelta(seconds=start_offset)
        lines.append(_inbound(start, msg))
        for i, (offset, latency, out, pct) in enumerate(calls, start=1):
            lines.append(_api_call(start + timedelta(seconds=offset), i, latency, out, pct))
        lines.append(_turn_ended(start + timedelta(seconds=calls[-1][0] + 0.5), len(calls)))
        end = start + timedelta(seconds=wait_s)
        lines.append(_response_ready(end, wait_s, len(calls)))
        turns_meta.append({"start": start, "end": end, "wait_s": wait_s, "calls": calls, "brain": brain})
    return lines, turns_meta


def _seed_usage_db(path: Path, turns_meta, actor="hermes-wa", extra_rows=()):
    """Build a real brain-usage.sqlite3 with the installed schema/writer, then close it.

    ``extra_rows`` is ``[(datetime, actor, outcome, error_code, duration_ms), ...]``.
    """

    current = {"t": BASE}
    log = BrainUsageLog(path, clock=lambda: current["t"])
    try:
        for meta in turns_meta:
            for offset, outcome, error_code, duration_ms in meta["brain"]:
                current["t"] = meta["start"] + timedelta(seconds=offset)
                log.record(BrainUsageEvent(
                    actor=actor, transport="mcp", operation="memory.op",
                    outcome=outcome, error_code=error_code, duration_ms=duration_ms,
                ))
        for dt, row_actor, outcome, error_code, duration_ms in extra_rows:
            current["t"] = dt
            log.record(BrainUsageEvent(
                actor=row_actor, transport="mcp", operation="memory.op",
                outcome=outcome, error_code=error_code, duration_ms=duration_ms,
            ))
    finally:
        log.close()


def _write_log(tmp_path: Path, lines: list[str], name="agent.log") -> Path:
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# End-to-end: the audit's four turns, in shape.
# ---------------------------------------------------------------------------

def test_four_turns_text_report_matches_the_audits_shape(tmp_path):
    lines, turns_meta = _build_log_lines()
    log_path = _write_log(tmp_path, lines)
    db_path = tmp_path / "brain-usage.sqlite3"
    _seed_usage_db(db_path, turns_meta)

    turns, warnings = damien_latency.build_turns(damien_latency.read_log_events([log_path]))
    assert warnings == []
    assert len(turns) == 4
    assert [t.complete for t in turns] == [True, True, True, True]
    assert [len(t.model_calls) for t in turns] == [4, 5, 3, 2]
    assert [t.wait_s for t in turns] == [44.2, 51.0, 28.0, 11.6]

    calls = damien_latency.read_brain_calls(db_path, "hermes-wa")
    for turn in turns:
        turn.brain = damien_latency.brain_window(calls, turn.start, turn.end)

    # Brain time matches the audit narrative ("~2s", "5.9s", "2.7s", "2.3s") in shape.
    assert [round(t.brain_time_s, 3) for t in turns] == [2.0, 5.89, 2.672, 2.3]
    assert turns[0].brain.refusal_codes == ()
    assert turns[1].brain.refusal_codes == ("brain.time.unresolved",)
    assert turns[2].brain.refusal_codes == ("brain.time.unresolved",)
    assert turns[3].brain.refusal_codes == ()

    # Exactly one cache miss, on turn 2's first call, as in the audit.
    misses = [(i, call.call_number) for i, t in enumerate(turns, 1) for call in t.model_calls if call.cache_miss]
    assert misses == [(2, 1)]

    summary = damien_latency.summarize(turns, brain_measured=True)
    waits = [spec[2] for spec in TURN_SPECS]
    call_counts = [len(spec[1]) for spec in TURN_SPECS]
    total_model_time = sum(c[1] for spec in TURN_SPECS for c in spec[1])
    assert summary["median_wait_s"] == statistics.median(waits)
    assert summary["model_calls_per_turn_mean"] == statistics.mean(call_counts)
    # Recomputed via a different summation order (flat vs. per-turn-then-total), so
    # compare with tolerance rather than assuming bit-identical float addition.
    assert summary["share_of_time_in_model_pct"] == pytest.approx(total_model_time / sum(waits) * 100)
    assert summary["cache_misses"] == 1

    text = damien_latency.render_text(turns, summary, warnings)
    assert "Turn 1" in text and "Turn 4" in text
    assert "CACHE MISS" in text
    assert "brain.time.unresolved" in text
    assert "21:42:10 SGT" in text  # UTC+8 of 13:42:10 UTC


def test_privacy_never_prints_a_fixtures_message_text(tmp_path):
    lines, turns_meta = _build_log_lines()
    log_path = _write_log(tmp_path, lines)
    db_path = tmp_path / "brain-usage.sqlite3"
    _seed_usage_db(db_path, turns_meta)

    for use_json in (False, True):
        argv = ["--log", str(log_path), "--usage-db", str(db_path)]
        if use_json:
            argv.append("--json")
        captured = _run_main_capture(argv)
        for message in ALL_MESSAGES:
            assert message not in captured, f"leaked message text with json={use_json}"
        assert "TESTMSG" not in captured
        assert "Nic W" not in captured  # the inbound line's user field is not printed either.


def test_json_output_structure_and_numbers(tmp_path):
    lines, turns_meta = _build_log_lines()
    log_path = _write_log(tmp_path, lines)
    db_path = tmp_path / "brain-usage.sqlite3"
    _seed_usage_db(db_path, turns_meta)

    captured = _run_main_capture(["--log", str(log_path), "--usage-db", str(db_path), "--json"])
    payload = json.loads(captured)
    assert len(payload["turns"]) == 4
    first = payload["turns"][0]
    assert first["start_utc"] == "2026-09-26T13:42:10+00:00"
    assert first["start_sgt"] == "2026-09-26T21:42:10+08:00"
    assert first["wait_s"] == 44.2
    assert first["brain"]["refusal_codes"] == []
    second = payload["turns"][1]
    assert second["brain"]["refusal_codes"] == ["brain.time.unresolved"]
    assert any(call["cache_miss"] for call in second["model_calls"])
    assert payload["summary"]["cache_misses"] == 1
    assert payload["warnings"] == []


def test_agent_log_alone_gives_the_full_report(tmp_path):
    """The live host's real primary source: one file, everything in it."""

    lines, _meta = _build_log_lines()
    agent_log = _write_log(tmp_path, lines, name="agent.log")

    payload = json.loads(_run_main_capture(["--log", str(agent_log), "--json"]))
    assert [turn["wait_s"] for turn in payload["turns"]] == [44.2, 51.0, 28.0, 11.6]
    assert [len(turn["model_calls"]) for turn in payload["turns"]] == [4, 5, 3, 2]
    assert payload["warnings"] == []


# ---------------------------------------------------------------------------
# Rotated files, in any order; and de-duplicating gateway.log/agent.log overlap.
# ---------------------------------------------------------------------------

def test_rotated_log_files_merge_regardless_of_argument_order(tmp_path):
    lines, _meta = _build_log_lines()
    midpoint = len(lines) // 2
    older = _write_log(tmp_path, lines[:midpoint], name="agent.log.1")
    newer = _write_log(tmp_path, lines[midpoint:], name="agent.log")

    # Pass the newer-named file first: the tool must sort by timestamp, not argv order.
    turns, warnings = damien_latency.build_turns(
        damien_latency.read_log_events([newer, older])
    )
    assert warnings == []
    assert [t.wait_s for t in turns] == [44.2, 51.0, 28.0, 11.6]
    assert turns == sorted(turns, key=lambda t: t.start)


def test_read_log_events_deduplicates_identical_lines_across_files(tmp_path):
    line = _inbound(BASE, "TESTMSG_DEDUP")
    first = _write_log(tmp_path, [line], name="a.log")
    second = _write_log(tmp_path, [line], name="b.log")  # byte-identical duplicate

    events = damien_latency.read_log_events([first, second])
    assert events == [damien_latency._Inbound(BASE, CHAT)]


def test_overlapping_gateway_and_agent_logs_do_not_duplicate_turns(tmp_path):
    """Reproduces the real find: gateway.log's inbound/response-ready lines are
    byte-identical copies of a subset of agent.log's. Passing both used to
    report every turn twice (one incomplete, one complete); it must not."""

    lines, _meta = _build_log_lines()
    agent_log = _write_log(tmp_path, lines, name="agent.log")
    gateway_log = _write_log(tmp_path, _gateway_only_lines(lines), name="gateway.log")

    for ordered in ([gateway_log, agent_log], [agent_log, gateway_log]):
        turns, warnings = damien_latency.build_turns(damien_latency.read_log_events(ordered))
        assert warnings == []
        assert len(turns) == 4
        assert [t.complete for t in turns] == [True, True, True, True]
        assert [len(t.model_calls) for t in turns] == [4, 5, 3, 2]
        assert [t.wait_s for t in turns] == [44.2, 51.0, 28.0, 11.6]


# ---------------------------------------------------------------------------
# --since / --until and --actor.
# ---------------------------------------------------------------------------

def test_since_until_filters_by_turn_start(tmp_path):
    lines, _meta = _build_log_lines()
    log_path = _write_log(tmp_path, lines)
    since = (BASE + timedelta(seconds=TURN_STARTS[1])).isoformat()
    until = (BASE + timedelta(seconds=TURN_STARTS[3]) - timedelta(seconds=1)).isoformat()

    # The window starts exactly at turn 2 and ends 1s before turn 4: only turns 2 and 3
    # (unique by wait_s: 51.0 and 28.0) should survive, turns 1 and 4 should not.
    captured = _run_main_capture(["--log", str(log_path), "--since", since, "--until", until, "--json"])
    payload = json.loads(captured)
    assert [turn["wait_s"] for turn in payload["turns"]] == [51.0, 28.0]


def test_since_drops_events_before_association_so_old_orphans_produce_no_note(tmp_path):
    """Reproduces the real find: an old (pre-window) model call with no open
    turn used to print an "outside any open turn" note even under --since,
    because the window was only applied to the finished turns list. It must
    now be dropped before association, with no note at all."""

    lines, _meta = _build_log_lines()
    old_orphan = _api_call(BASE - timedelta(days=7), 999, 1.0, 10, 90, session="old-sess")
    log_path = _write_log(tmp_path, [old_orphan] + lines)

    # Sanity check: with no --since, that old call really is an unattributable orphan.
    turns_all, warnings_all = damien_latency.build_turns(damien_latency.read_log_events([log_path]))
    assert len(turns_all) == 4
    assert any("999" in warning for warning in warnings_all)

    # With --since at turn 1's start, the old call is dropped before it is ever
    # considered for attribution: no note, and the real turns are unaffected.
    turns, warnings = damien_latency.build_turns(damien_latency.read_log_events([log_path], since=BASE))
    assert warnings == []
    assert [t.wait_s for t in turns] == [44.2, 51.0, 28.0, 11.6]

    # Same, through the CLI end to end.
    payload = json.loads(_run_main_capture(["--log", str(log_path), "--since", BASE.isoformat(), "--json"]))
    assert payload["warnings"] == []
    assert [turn["wait_s"] for turn in payload["turns"]] == [44.2, 51.0, 28.0, 11.6]


def test_actor_filter_excludes_other_actors_brain_calls(tmp_path):
    lines, turns_meta = _build_log_lines()
    log_path = _write_log(tmp_path, lines)
    db_path = tmp_path / "brain-usage.sqlite3"
    other_actor_row = (turns_meta[0]["start"] + timedelta(seconds=1.5), "local-codex", "ok", None, 5000)
    _seed_usage_db(db_path, turns_meta, actor="hermes-wa", extra_rows=[other_actor_row])

    turns, _warnings = damien_latency.build_turns(damien_latency.read_log_events([log_path]))

    default_calls = damien_latency.read_brain_calls(db_path, damien_latency.DEFAULT_ACTOR)
    window = damien_latency.brain_window(default_calls, turns[0].start, turns[0].end)
    assert window.calls == 4  # the four hermes-wa rows only; local-codex's row is excluded.

    other_calls = damien_latency.read_brain_calls(db_path, "local-codex")
    other_window = damien_latency.brain_window(other_calls, turns[0].start, turns[0].end)
    assert other_window.calls == 1
    assert other_window.total_ms == 5000


# ---------------------------------------------------------------------------
# --usage-json: the two-account way to get the joined report when one account
# cannot open both gateway/agent.log (nic) and brain-usage.sqlite3 (gremlin-town).
# ---------------------------------------------------------------------------

def _export_usage_json(db_path: Path, actor: str) -> str:
    """What the documented ``sudo -u gremlin-town python3 -c ...`` one-liner produces."""

    calls = damien_latency.read_brain_calls(db_path, actor)
    return json.dumps([
        {"at": at.isoformat(), "outcome": outcome, "error_code": error_code, "duration_ms": duration_ms}
        for at, outcome, error_code, duration_ms in calls
    ])


def test_usage_json_file_matches_usage_db(tmp_path):
    lines, turns_meta = _build_log_lines()
    log_path = _write_log(tmp_path, lines)
    db_path = tmp_path / "brain-usage.sqlite3"
    _seed_usage_db(db_path, turns_meta)
    json_path = tmp_path / "usage-export.json"
    json_path.write_text(_export_usage_json(db_path, "hermes-wa"), encoding="utf-8")

    from_db = json.loads(_run_main_capture(["--log", str(log_path), "--usage-db", str(db_path), "--json"]))
    from_json = json.loads(_run_main_capture(["--log", str(log_path), "--usage-json", str(json_path), "--json"]))
    assert [turn["brain"] for turn in from_json["turns"]] == [turn["brain"] for turn in from_db["turns"]]
    assert from_json["summary"]["brain_measured"] is True


def test_usage_json_via_stdin_matches_the_documented_two_account_shape(tmp_path):
    """The second account's export piped straight in, as the docs show it."""

    lines, turns_meta = _build_log_lines()
    log_path = _write_log(tmp_path, lines)
    db_path = tmp_path / "brain-usage.sqlite3"
    _seed_usage_db(db_path, turns_meta)
    json_path = tmp_path / "usage-export.json"
    json_path.write_text(_export_usage_json(db_path, "hermes-wa"), encoding="utf-8")

    with json_path.open("rb") as usage_stdin:
        result = subprocess.run(
            [sys.executable, str(SCRIPT), "--log", str(log_path), "--usage-json", "-", "--json"],
            stdin=usage_stdin, capture_output=True, text=True, check=False,
        )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["turns"][1]["brain"]["refusal_codes"] == ["brain.time.unresolved"]
    assert payload["summary"]["brain_measured"] is True


def test_usage_db_and_usage_json_are_mutually_exclusive(tmp_path, capsys):
    lines, _meta = _build_log_lines()
    log_path = _write_log(tmp_path, lines)

    exit_code = damien_latency.main([
        "--log", str(log_path), "--usage-db", "a.sqlite3", "--usage-json", "b.json",
    ])
    assert exit_code == 1
    assert "mutually exclusive" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Robustness: incomplete turns and ambiguous/orphaned events never crash.
# ---------------------------------------------------------------------------

def test_incomplete_turn_is_reported_and_dropped_from_wait_summary(tmp_path):
    lines = [_inbound(BASE, "TESTMSG_NEVER_ANSWERED")]
    log_path = _write_log(tmp_path, lines)

    captured = _run_main_capture(["--log", str(log_path), "--json"])
    payload = json.loads(captured)
    assert len(payload["turns"]) == 1
    turn = payload["turns"][0]
    assert turn["complete"] is False
    assert turn["wait_s"] is None
    assert "incomplete" in turn["warnings"][0]
    assert payload["summary"]["median_wait_s"] is None
    assert payload["summary"]["turns_incomplete"] == 1
    assert "TESTMSG_NEVER_ANSWERED" not in captured


def test_brain_window_dedupes_repeated_refusal_codes():
    start = BASE
    end = BASE + timedelta(seconds=10)
    calls = [
        (start + timedelta(seconds=1), "error", "brain.time.unresolved", 100),
        (start + timedelta(seconds=2), "error", "brain.time.unresolved", 120),
        (start + timedelta(seconds=3), "error", "ACCESS_DENIED", 50),
        (start + timedelta(seconds=4), "ok", None, 200),
    ]
    window = damien_latency.brain_window(calls, start, end)
    assert window.calls == 4
    assert window.total_ms == 470
    assert window.refusal_codes == ("ACCESS_DENIED", "brain.time.unresolved")  # sorted, deduped


def test_ambiguous_concurrent_turns_attach_to_the_older_one_with_a_warning():
    t0 = BASE
    events = [
        damien_latency._Inbound(t0, "chat-a"),
        damien_latency._Inbound(t0 + timedelta(seconds=1), "chat-b"),
        # a model-call session never seen before, while two turns are open at once.
        damien_latency._ApiCall(t0 + timedelta(seconds=2), "unseen-session", 1, "grok-4.7", 50, 1.0, 90),
        damien_latency._ResponseReady(t0 + timedelta(seconds=5), "chat-a", 5.0),
        damien_latency._ResponseReady(t0 + timedelta(seconds=6), "chat-b", 5.0),
    ]
    turns, warnings = damien_latency.build_turns(events)
    assert len(turns) == 2
    assert warnings == []  # the ambiguity is attached to the turn itself, not lost.
    older = next(t for t in turns if t.start == t0)
    assert len(older.model_calls) == 1
    assert any("ambiguous" in note for note in older.warnings)
    newer = next(t for t in turns if t.start != t0)
    assert newer.model_calls == []


def test_orphan_response_ready_and_unattributed_call_are_noted_not_dropped_silently():
    t0 = BASE
    events = [
        damien_latency._ResponseReady(t0, "chat-x", 3.0),  # no inbound ever opened this chat
        damien_latency._ApiCall(t0 + timedelta(seconds=1), "sess", 1, "grok-4.7", 10, 1.0, 90),  # no turn open at all
    ]
    turns, warnings = damien_latency.build_turns(events)
    assert turns == []
    assert len(warnings) == 2
    assert any("response ready" in w for w in warnings)
    assert any("model call" in w for w in warnings)


# ---------------------------------------------------------------------------
# The documented invocation shape: piped via stdin, exactly like the hub command.
# ---------------------------------------------------------------------------

def test_cli_via_stdin_matches_the_documented_command_shape(tmp_path):
    lines, _meta = _build_log_lines()
    log_path = _write_log(tmp_path, lines)

    with SCRIPT.open("rb") as script_stdin:
        result = subprocess.run(
            [sys.executable, "-", "--log", str(log_path), "--json"],
            stdin=script_stdin, capture_output=True, text=True, check=False,
        )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload["turns"]) == 4
    for message in ALL_MESSAGES:
        assert message not in result.stdout


def test_missing_log_file_fails_loudly_with_nonzero_exit(tmp_path, capsys):
    missing = tmp_path / "does-not-exist.log"
    exit_code = damien_latency.main(["--log", str(missing)])
    assert exit_code == 1
    captured = capsys.readouterr()
    assert "damien-latency: refused" in captured.err
    assert captured.out == ""


def _run_main_capture(argv: list[str]) -> str:
    """Run ``main`` in-process and return whatever it printed to stdout."""

    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        exit_code = damien_latency.main(argv)
    assert exit_code == 0
    return buffer.getvalue()
