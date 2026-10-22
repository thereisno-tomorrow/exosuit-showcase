"""Human argument adapter and versioned JSON stdin/stdout entry point."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from typing import Any

from brain_core import __version__
from brain_core.errors import BrainError, ErrorCode
from brain_core.operations import dispatch
from brain_core.protocol import (
    PROTOCOL_VERSION,
    ProtocolRequest,
    error_response,
    parse_request,
    render_json,
    success_response,
)


class BrainArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BrainError(ErrorCode.INVALID_ARGUMENT, message, details={"usage": self.format_usage().strip()})


def _catalog_arguments(parser: argparse.ArgumentParser) -> None:
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--node-map")
    source.add_argument("--legacy-registry")
    parser.add_argument("--node")


def _output_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", dest="json_output")
    parser.add_argument("--request-id", default="cli-01")


def _grant_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--grant-file", action="append", default=[], dest="grant_files")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--now")
    parser.add_argument("--mode", default="think")


def _brain_home_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--brain-home", required=True)


def _evidence_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--evidence-file", required=True)


def _write_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--now")
    parser.add_argument("--entropy")


def build_parser() -> BrainArgumentParser:
    parser = BrainArgumentParser(prog="brain")
    parser.add_argument("--version", action="version", version=f"brain {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=BrainArgumentParser)

    commands.add_parser("json", help="read one versioned request from stdin")

    doctor = commands.add_parser("doctor")
    doctor.set_defaults(operation="doctor")
    _output_arguments(doctor)

    vault = commands.add_parser("vault")
    vault_commands = vault.add_subparsers(dest="vault_command", required=True, parser_class=BrainArgumentParser)
    discover = vault_commands.add_parser("discover")
    discover.set_defaults(operation="vault.discover")
    _catalog_arguments(discover)
    _output_arguments(discover)

    index = commands.add_parser("index")
    index_commands = index.add_subparsers(dest="index_command", required=True, parser_class=BrainArgumentParser)
    rebuild = index_commands.add_parser("rebuild")
    rebuild.set_defaults(operation="index.rebuild")
    _catalog_arguments(rebuild)
    rebuild.add_argument("--database", required=True)
    _grant_arguments(rebuild)
    rebuild.add_argument("--lock-timeout", type=float, default=1.0)
    _output_arguments(rebuild)
    status = index_commands.add_parser("status")
    status.set_defaults(operation="index.status")
    status.add_argument("--database", required=True)
    _output_arguments(status)

    context = commands.add_parser("context")
    context_commands = context.add_subparsers(dest="context_command", required=True, parser_class=BrainArgumentParser)
    for name in ("plan", "build"):
        context_parser = context_commands.add_parser(name)
        context_parser.set_defaults(operation=f"context.{name}")
        _catalog_arguments(context_parser)
        context_parser.add_argument("--database", required=True)
        context_parser.add_argument("--intent", required=True)
        context_parser.add_argument("--vault", action="append", default=[], dest="vaults")
        context_parser.add_argument("--facet", action="append", default=[], dest="facets")
        context_parser.add_argument("--source-tokens", type=int, default=12_000)
        context_parser.add_argument("--vault-budget", type=int, default=4)
        context_parser.add_argument("--excerpt-budget", type=int, default=12)
        context_parser.add_argument("--expansion-facet")
        context_parser.add_argument("--allow-stale", action="store_true")
        _grant_arguments(context_parser)
        if name == "build":
            context_parser.add_argument("--receipt-dir")
        _output_arguments(context_parser)
    inspect = context_commands.add_parser("inspect")
    inspect.set_defaults(operation="context.inspect")
    inspect.add_argument("--receipt-dir", required=True)
    inspect.add_argument("--pack-id", required=True)
    _output_arguments(inspect)

    skills = commands.add_parser("skills")
    skill_commands = skills.add_subparsers(dest="skills_command", required=True, parser_class=BrainArgumentParser)
    for name in ("list", "check"):
        skill_parser = skill_commands.add_parser(name)
        skill_parser.set_defaults(operation=f"skills.{name}")
        skill_parser.add_argument("--skill-path", action="append", default=[], dest="skill_paths")
        _output_arguments(skill_parser)
    route = skill_commands.add_parser("route")
    route.set_defaults(operation="skills.route")
    route.add_argument("--skill-path", action="append", default=[], dest="skill_paths")
    route.add_argument("--intent", required=True)
    route.add_argument("--mode", default="think")
    route.add_argument("--explicit-skill-id")
    route.add_argument("--authority", action="append", default=[])
    route.add_argument("--available-dependency", action="append", default=[], dest="available_dependencies")
    route.add_argument("--host-capability", action="append", default=[], dest="host_capabilities")
    route.add_argument("--project-owner")
    route.add_argument("--load-instructions", action="store_true")
    _output_arguments(route)

    record = commands.add_parser("record")
    record_commands = record.add_subparsers(dest="record_command", required=True, parser_class=BrainArgumentParser)
    create = record_commands.add_parser("create")
    create.set_defaults(operation="record.create")
    _brain_home_argument(create)
    create.add_argument("--record-file", required=True)
    create.add_argument("--actor")
    _output_arguments(create)
    list_records = record_commands.add_parser("list")
    list_records.set_defaults(operation="record.list", current=True)
    _brain_home_argument(list_records)
    list_records.add_argument("--kind")
    list_records.add_argument("--history", action="store_false", dest="current")
    _output_arguments(list_records)
    show = record_commands.add_parser("show")
    show.set_defaults(operation="record.show")
    _brain_home_argument(show)
    show.add_argument("--record-id", required=True)
    _output_arguments(show)
    transition = record_commands.add_parser("transition")
    transition.set_defaults(operation="record.transition")
    _brain_home_argument(transition)
    transition.add_argument("--record-id", required=True)
    transition.add_argument("--state")
    transition.add_argument("--data-file", dest="data_file")
    transition.add_argument("--source-file", dest="source_file")
    transition.add_argument("--actor")
    transition.add_argument("--expected-sha256", dest="expected_sha256")
    _evidence_argument(transition)
    _write_arguments(transition)
    _output_arguments(transition)

    continuity = commands.add_parser("continuity")
    continuity_commands = continuity.add_subparsers(dest="continuity_command", required=True, parser_class=BrainArgumentParser)
    continuity_start = continuity_commands.add_parser("start")
    continuity_start.set_defaults(operation="continuity.start")
    _brain_home_argument(continuity_start)
    continuity_start.add_argument("--session-id", required=True)
    continuity_start.add_argument("--intent", required=True)
    continuity_start.add_argument("--source", action="append", default=[], dest="sources")
    continuity_start.add_argument("--next-step", default="Continue the declared intent.")
    continuity_start.add_argument("--task-id", dest="task_id", help="the record ID of the task this note is for")
    _evidence_argument(continuity_start)
    _write_arguments(continuity_start)
    _output_arguments(continuity_start)
    continuity_append = continuity_commands.add_parser("append")
    continuity_append.set_defaults(operation="continuity.append", meaningful=True)
    _brain_home_argument(continuity_append)
    continuity_append.add_argument("--session-id", required=True)
    continuity_append.add_argument("--event", required=True)
    continuity_append.add_argument("--value", required=True)
    continuity_append.add_argument("--not-meaningful", action="store_false", dest="meaningful")
    _evidence_argument(continuity_append)
    _write_arguments(continuity_append)
    _output_arguments(continuity_append)
    continuity_close = continuity_commands.add_parser("close")
    continuity_close.set_defaults(operation="continuity.close", recovered=False)
    _brain_home_argument(continuity_close)
    continuity_close.add_argument("--session-id", required=True)
    continuity_close.add_argument("--recovered", action="store_true")
    _evidence_argument(continuity_close)
    _write_arguments(continuity_close)
    _output_arguments(continuity_close)
    continuity_recover = continuity_commands.add_parser("recover")
    continuity_recover.set_defaults(operation="continuity.recover")
    _brain_home_argument(continuity_recover)
    continuity_recover.add_argument("--max-tokens", type=int, default=2_500)
    continuity_recover.add_argument("--session-id", dest="session_id", help="recover this one note instead of the latest")
    _output_arguments(continuity_recover)

    memory = commands.add_parser("memory")
    memory_commands = memory.add_subparsers(dest="memory_command", required=True, parser_class=BrainArgumentParser)
    clearout_plan = memory_commands.add_parser("clearout-plan")
    clearout_plan.set_defaults(operation="memory.clearout.plan")
    clearout_plan.add_argument("--root", required=True)
    _output_arguments(clearout_plan)

    time_parser = commands.add_parser("time")
    time_commands = time_parser.add_subparsers(dest="time_command", required=True, parser_class=BrainArgumentParser)
    resolve = time_commands.add_parser("resolve")
    resolve.set_defaults(operation="time.resolve")
    resolve.add_argument("--words", required=True)
    resolve.add_argument("--now")
    _output_arguments(resolve)

    decisions = commands.add_parser("decisions")
    decision_commands = decisions.add_subparsers(dest="decisions_command", required=True, parser_class=BrainArgumentParser)
    import_parser = decision_commands.add_parser("import")
    import_parser.set_defaults(operation="decisions.import", apply=False)
    import_parser.add_argument("--source", required=True)
    import_parser.add_argument("--brain-home")
    import_parser.add_argument("--apply", action="store_true")
    _output_arguments(import_parser)
    return parser


def _parameters(namespace: argparse.Namespace) -> dict[str, Any]:
    ignored = {
        "command", "vault_command", "index_command", "context_command", "skills_command",
        "record_command", "continuity_command", "decisions_command", "memory_command", "time_command",
        "operation", "json_output", "request_id",
    }
    return {key: value for key, value in vars(namespace).items() if key not in ignored and value is not None}


def _human(data: dict[str, Any]) -> str:
    if set(data) >= {"version", "python", "sqlite", "fts5"}:
        return "\n".join(
            (
                f"Brain Core {data['version']}",
                f"Python {data['python']}",
                f"SQLite {data['sqlite']} (FTS5 {data['fts5']})",
                f"Phase {data.get('phase', 'unknown')}; model calls 0; network calls 0",
            )
        )
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def _emit(text: str, *, error: bool = False) -> None:
    """Write text, falling back to UTF-8 on legacy console encodings."""

    stream = sys.stderr if error else sys.stdout
    rendered = f"{text}\n"
    try:
        stream.write(rendered)
    except UnicodeEncodeError:
        stream.buffer.write(rendered.encode("utf-8"))
        stream.buffer.flush()


def _run_json() -> int:
    request = parse_request(sys.stdin.read())
    data = dispatch(request)
    _emit(render_json(success_response(request.request_id, data)))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    raw_arguments = list(sys.argv[1:] if argv is None else argv)
    json_requested = "--json" in raw_arguments or (raw_arguments[:1] == ["json"])
    request_id: str | None = None
    try:
        namespace = build_parser().parse_args(raw_arguments)
        if namespace.command == "json":
            return _run_json()
        request_id = namespace.request_id
        request = ProtocolRequest(
            protocol_version=PROTOCOL_VERSION,
            request_id=request_id,
            operation=namespace.operation,
            parameters=_parameters(namespace),
        )
        data = dispatch(request)
        if namespace.json_output:
            _emit(render_json(success_response(request.request_id, data)))
        else:
            _emit(_human(data))
        return 0
    except BrainError as exc:
        if json_requested:
            _emit(render_json(error_response(exc, request_id)))
        else:
            _emit(f"{exc.code.value}: {exc.message}", error=True)
            if exc.hint:
                _emit(f"Hint: {exc.hint}", error=True)
        return exc.exit_code
