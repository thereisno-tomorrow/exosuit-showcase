import asyncio
import json
from datetime import UTC, datetime, timedelta

from gremlin_town.contracts import Actor
from gremlin_town.entrypoint import main as entrypoint_main
from gremlin_town.mcp.brain_tools import build_brain_tools
from gremlin_town.mcp.brain_usage import BrainUsageEvent, BrainUsageLog, read_usage, render_markdown
from tests.mcp.test_brain_tools import Runtime, allow

SECRET_BODY = "Prepare the private Friday decision deck."


def logged_tools(tmp_path, scopes=frozenset({"bc:read", "bc:write"})):
    log = BrainUsageLog(tmp_path / "brain-usage.sqlite3")
    definitions = build_brain_tools(
        Runtime(), lambda: Actor("alice"), scopes_provider=lambda: scopes,
        authorize=allow, usage_log=log, transport="http",
    )
    return log, {tool.name: tool for tool in definitions}


def test_calls_are_logged_with_metadata_only(tmp_path):
    log, tools = logged_tools(tmp_path)
    asyncio.run(tools["bc_memory_save"].run({
        "operation_id": "save-1", "scope": "personal", "kind": "item", "level": "accepted",
        "said_by": "owner", "came_by": "direct", "words": SECRET_BODY,
        "title": "Private title", "body": SECRET_BODY,
    }))
    asyncio.run(tools["bc_memory_read"].run({"scope": "personal", "record_id": "brn_20260921_120000_abcd"}))
    log.close()

    rows = read_usage(tmp_path / "brain-usage.sqlite3")
    assert [(r["actor"], r["transport"], r["operation"], r["outcome"]) for r in rows] == [
        ("alice", "http", "memory.save", "ok"),
        ("alice", "http", "memory.read", "ok"),
    ]
    assert rows[0]["operation_id"] == "save-1"
    assert rows[0]["record_id"] == "brn_20260921_120000_abcd"
    assert rows[1]["operation_id"] is None
    raw = (tmp_path / "brain-usage.sqlite3").read_bytes()
    assert SECRET_BODY.encode() not in raw and b"Private title" not in raw


def test_refusals_are_logged_with_error_code(tmp_path):
    log, tools = logged_tools(tmp_path, scopes=frozenset({"bc:read"}))
    result = asyncio.run(tools["bc_memory_save"].run({
        "operation_id": "save-2", "scope": "personal", "kind": "item", "level": "accepted",
        "said_by": "owner", "came_by": "direct", "words": "b", "title": "t", "body": "b",
    }))
    assert result.isError
    log.close()
    (row,) = read_usage(tmp_path / "brain-usage.sqlite3")
    assert (row["outcome"], row["error_code"], row["operation_id"]) == ("error", "ACCESS_DENIED", "save-2")


def test_log_failure_never_changes_the_brain_result(tmp_path, caplog):
    log, tools = logged_tools(tmp_path)
    log.close()  # every later write fails
    result = asyncio.run(tools["bc_memory_read"].run({"scope": "personal", "record_id": "brn_20260921_120000_abcd"}))
    assert not result.isError
    assert "Brain usage log write failed" in caplog.text


def test_unsafe_values_are_not_stored_and_old_rows_expire(tmp_path):
    now = datetime(2026, 9, 25, tzinfo=UTC)
    path = tmp_path / "log.sqlite3"
    old = BrainUsageLog(path, clock=lambda: now - timedelta(days=200))
    old.record(BrainUsageEvent("alice", "mcp", "memory.read", "ok", None, 5))
    old.close()
    log = BrainUsageLog(path, clock=lambda: now)
    log.record(BrainUsageEvent("alice", "mcp", "bad operation name!", "error", "not a code", 7))
    log.close()
    (row,) = read_usage(path)
    assert (row["operation"], row["error_code"]) == ("unknown", None)


def test_markdown_groups_by_day_agent_and_transport(tmp_path):
    rows = [
        {"at": "2026-09-25T06:37:00+00:00", "actor": "local-codex", "transport": "http", "operation": "memory.save",
         "outcome": "ok", "error_code": None, "duration_ms": 31000},
        {"at": "2026-09-25T06:38:00+00:00", "actor": "local-codex", "transport": "http", "operation": "memory.save",
         "outcome": "ok", "error_code": None, "duration_ms": 900},
        {"at": "2026-09-25T06:39:00+00:00", "actor": "local-codex", "transport": "http", "operation": "memory.search",
         "outcome": "error", "error_code": "brain.invalid_argument", "duration_ms": 12},
    ]
    assert render_markdown(rows) == (
        "| 2026-09-25 | local-codex (http, server log) | `memory.save` ×2, `memory.search` "
        "| 2 ok, 1 error; slowest 31.0 s | `brain.invalid_argument` |\n"
    )
    assert render_markdown([]) == ""


def test_operator_command_prints_rows(tmp_path, capsys):
    log, tools = logged_tools(tmp_path)
    asyncio.run(tools["bc_memory_read"].run({"scope": "personal", "record_id": "brn_20260921_120000_abcd"}))
    log.close()
    database = str(tmp_path / "brain-usage.sqlite3")
    assert entrypoint_main(["brain-usage", "--database", database]) == 0
    assert "| alice (http, server log) | `memory.read` | 1 ok" in capsys.readouterr().out
    assert entrypoint_main(["brain-usage", "--database", database, "--format", "json", "--since", "2000-01-01"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["operation"] == "memory.read"
    assert entrypoint_main(["brain-usage", "--database", str(tmp_path / "missing.sqlite3")]) == 2
