from __future__ import annotations

import hashlib
import io
import json
import runpy
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

import brain_core.cli as cli
from brain_core.errors import BrainError, ErrorCode
from brain_core.records import RECORD_SCHEMA_V2, new_record_id

NOW = datetime(2026, 9, 26, 13, 0, 0, tzinfo=UTC)


def test_parser_builds_all_commands_and_version(capsys) -> None:
    parser = cli.build_parser()
    parsed = parser.parse_args(
        [
            "context",
            "build",
            "--node-map",
            "node.yaml",
            "--database",
            "brain.db",
            "--intent",
            "compare",
            "--session-id",
            "session",
            "--vault",
            "vault_career",
            "--facet",
            "launch",
            "--receipt-dir",
            "receipts",
        ]
    )
    parameters = cli._parameters(parsed)
    assert parameters["vaults"] == ["vault_career"]
    assert parameters["facets"] == ["launch"]
    assert "operation" not in parameters

    with pytest.raises(SystemExit) as raised:
        parser.parse_args(["--version"])
    assert raised.value.code == 0
    assert "brain 0.4.0" in capsys.readouterr().out


def test_argument_errors_are_brain_errors() -> None:
    with pytest.raises(BrainError) as raised:
        cli.build_parser().parse_args(["context", "inspect"])
    assert raised.value.code is ErrorCode.INVALID_ARGUMENT
    assert "usage" in raised.value.details


def test_human_rendering_has_doctor_summary_and_generic_json() -> None:
    doctor = cli._human({"version": "1", "python": "3.14", "sqlite": "3", "fts5": "ok"})
    assert "Brain Core 1" in doctor
    assert "model calls 0" in doctor
    assert cli._human({"z": 2, "a": 1}).splitlines()[1].strip() == '"a": 1,'


def test_emit_falls_back_to_utf8_for_legacy_console_encoding(monkeypatch) -> None:
    class LegacyConsole:
        def __init__(self) -> None:
            self.buffer = io.BytesIO()

        def write(self, text: str) -> None:
            text.encode("cp1252")

    console = LegacyConsole()
    monkeypatch.setattr(cli.sys, "stdout", console)
    cli._emit("evidence → positioning")
    assert console.buffer.getvalue() == "evidence → positioning\n".encode("utf-8")


def test_main_human_and_json_outputs(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "dispatch", lambda request: {"operation": request.operation})
    assert cli.main(["doctor"]) == 0
    assert '"operation": "doctor"' in capsys.readouterr().out

    assert cli.main(["doctor", "--json", "--request-id", "request-cli"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["request_id"] == "request-cli"
    assert envelope["data"] == {"operation": "doctor"}


def test_main_uses_process_arguments_when_argv_is_none(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["brain", "doctor"])
    monkeypatch.setattr(cli, "dispatch", lambda request: {"ok": request.operation})
    assert cli.main() == 0
    assert '"ok": "doctor"' in capsys.readouterr().out


def test_json_stdin_success(monkeypatch, capsys) -> None:
    raw = json.dumps(
        {
            "protocol_version": "1",
            "request_id": "stdin-request",
            "operation": "doctor",
            "parameters": {},
        }
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    monkeypatch.setattr(cli, "dispatch", lambda request: {"seen": request.operation})
    assert cli.main(["json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["data"] == {"seen": "doctor"}


def test_json_stdin_error_exit_and_envelope(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
    exit_code = cli.main(["json"])
    assert exit_code == BrainError(ErrorCode.PROTOCOL_INVALID, "x").exit_code
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["request_id"] is None
    assert envelope["error"]["code"] == ErrorCode.PROTOCOL_INVALID.value


def test_human_error_renders_hint_and_exit_code(monkeypatch, capsys) -> None:
    def failed(_request):
        raise BrainError(
            ErrorCode.CAPABILITY_UNAVAILABLE,
            "synthetic failure",
            hint="install the capability",
        )

    monkeypatch.setattr(cli, "dispatch", failed)
    assert cli.main(["doctor"]) == BrainError(ErrorCode.CAPABILITY_UNAVAILABLE, "x").exit_code
    output = capsys.readouterr()
    assert "synthetic failure" in output.err
    assert "Hint: install the capability" in output.err


def test_argument_error_without_hint_uses_stderr(capsys) -> None:
    exit_code = cli.main(["context", "inspect"])
    assert exit_code == BrainError(ErrorCode.INVALID_ARGUMENT, "x").exit_code
    output = capsys.readouterr()
    assert ErrorCode.INVALID_ARGUMENT.value in output.err
    assert "Hint:" not in output.err


def test_memory_clearout_plan_command_parses_and_dispatches(tmp_path: Path, capsys) -> None:
    (tmp_path / "records").mkdir()
    assert cli.main(["memory", "clearout-plan", "--root", str(tmp_path), "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["data"]["dry_run"] is True


def test_time_resolve_command_parses_and_dispatches(capsys) -> None:
    assert cli.main(["time", "resolve", "--words", "Friday", "--now", NOW.isoformat(), "--json"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["data"]["resolved"] == "2026-10-02"


def test_record_create_and_transition_v2_round_trip_through_the_cli(tmp_path: Path, capsys) -> None:
    words = "I'll pay the electricity bill Friday"
    record = {
        "schema": RECORD_SCHEMA_V2,
        "id": new_record_id(NOW, "aaaa01"),
        "kind": "item",
        "title": "pay the electricity bill",
        "state": "open",
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
        "sensitivity": "private",
        "vaults": [],
        "owners": ["nic"],
        "entities": [],
        "evidence": [
            {
                "source_type": "session_message",
                "locator": "session:cli:1",
                "content_sha256": hashlib.sha256(words.encode("utf-8")).hexdigest(),
                "excerpt": words,
            }
        ],
        "supersedes": [],
        "dedupe_key": None,
        "data": {"level": "accepted", "size": "task", "owed_to": "electricity company"},
        "body": "",
        "source": {"said_by": "owner", "came_by": "direct", "words": words, "recorded_by": "chatgpt-web"},
    }
    record_file = tmp_path / "record.json"
    record_file.write_text(json.dumps(record), encoding="utf-8")
    brain_home = tmp_path / "home"
    (brain_home / "records").mkdir(parents=True)

    assert (
        cli.main(
            [
                "record",
                "create",
                "--brain-home",
                str(brain_home),
                "--record-file",
                str(record_file),
                "--actor",
                "chatgpt-web",
                "--json",
            ]
        )
        == 0
    )
    created = json.loads(capsys.readouterr().out)["data"]
    record_id = created["record"]["id"]
    sha256 = created["receipt"]["content_sha256"]

    source_file = tmp_path / "source.json"
    source_file.write_text(
        json.dumps({"said_by": "agent", "came_by": "agent_guess", "speaker": "chatgpt-web", "words": "mark it waiting", "recorded_by": "chatgpt-web"}),
        encoding="utf-8",
    )
    evidence_file = tmp_path / "evidence.json"
    evidence_file.write_text(
        json.dumps(
            {
                "source_type": "session_message",
                "locator": "session:cli:2",
                "content_sha256": hashlib.sha256(b"mark it waiting").hexdigest(),
                "excerpt": "mark it waiting",
            }
        ),
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "record",
                "transition",
                "--brain-home",
                str(brain_home),
                "--record-id",
                record_id,
                "--state",
                "waiting",
                "--source-file",
                str(source_file),
                "--evidence-file",
                str(evidence_file),
                "--actor",
                "chatgpt-web",
                "--expected-sha256",
                sha256,
                "--now",
                NOW.isoformat(),
                "--json",
            ]
        )
        == 0
    )
    transitioned = json.loads(capsys.readouterr().out)["data"]
    assert transitioned["record"]["state"] == "waiting"


def test_continuity_start_takes_a_task_id_and_recover_a_session_id_through_the_cli(tmp_path: Path, capsys) -> None:
    """N12b: a note can name its task from the command line, and one note can be recovered by name."""

    evidence_file = tmp_path / "evidence.json"
    evidence_file.write_text(
        json.dumps({
            "source_type": "session_message", "locator": "session:cli:2",
            "content_sha256": hashlib.sha256(b"cli").hexdigest(), "excerpt": "cli",
        }),
        encoding="utf-8",
    )
    brain_home = tmp_path / "home"
    (brain_home / "records").mkdir(parents=True)
    task_id = new_record_id(NOW, "bbbb02")
    assert cli.main([
        "continuity", "start", "--brain-home", str(brain_home), "--session-id", "call-the-bank",
        "--intent", "Call the bank", "--task-id", task_id, "--evidence-file", str(evidence_file),
        "--now", NOW.isoformat(), "--entropy", "cccc03", "--json",
    ]) == 0
    started = json.loads(capsys.readouterr().out)["data"]
    assert started["record"]["data"]["task_id"] == task_id
    assert cli.main([
        "continuity", "recover", "--brain-home", str(brain_home), "--session-id", "call-the-bank", "--json",
    ]) == 0
    recovered = json.loads(capsys.readouterr().out)["data"]
    assert recovered["session_id"] == "call-the-bank"
    assert recovered["task_id"] == task_id


def test_module_entrypoint_exits_with_main_result(monkeypatch) -> None:
    monkeypatch.setattr(cli, "main", lambda: 7)
    with pytest.raises(SystemExit) as raised:
        runpy.run_module("brain_core.__main__", run_name="__main__")
    assert raised.value.code == 7
