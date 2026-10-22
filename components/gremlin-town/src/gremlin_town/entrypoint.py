"""Installed command entrypoint for the server and thin JSON client."""

from __future__ import annotations

import argparse
import ipaddress
import json
import socket
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any

import uvicorn

from gremlin_town.cli.main import main as client_main
from gremlin_town.recovery import RecoveryError, audit_recovery, restore_snapshot
from gremlin_town.runtime import build_runtime


class ListenerDispatch:
    """Route by the actual local socket, never by a client-controlled header."""

    def __init__(self, private_app: Any, public_app: Any = None, *, private_port: int,
                 public_port: int | None = None, mcp_app: Any = None,
                 mcp_port: int | None = None) -> None:
        if (public_app is None) != (public_port is None) or (mcp_app is None) != (mcp_port is None):
            raise ValueError("each listener requires both an app and port")
        ports = [port for port in (private_port, public_port, mcp_port) if port is not None]
        if len(set(ports)) != len(ports):
            raise ValueError("listeners must use different ports")
        self.private_app = private_app
        self.public_app = public_app
        self.private_port = private_port
        self.public_port = public_port
        self.mcp_app = mcp_app
        self.mcp_port = mcp_port

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            await self.private_app(scope, receive, send)
            return
        server = scope.get("server")
        destination_port = server[1] if isinstance(server, (tuple, list)) and len(server) >= 2 else None
        if self.mcp_app is not None and destination_port == self.mcp_port:
            await self.mcp_app(scope, receive, send)
            return
        if self.public_app is not None and destination_port == self.public_port:
            await self.public_app(scope, receive, send)
            return
        if destination_port == self.private_port:
            await self.private_app(scope, receive, send)
            return
        await send({"type": "http.response.start", "status": 404, "headers": []})
        await send({"type": "http.response.body", "body": b""})


def _loopback_override(host: str) -> str:
    try:
        allowed = ipaddress.ip_address(host).is_loopback
    except ValueError:
        allowed = host.lower() == "localhost"
    if not allowed:
        raise ValueError("Gremlin Town listeners must remain loopback-only")
    return host


def _listener(host: str, port: int) -> socket.socket:
    listener = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, port))
        listener.listen(2048)
    except BaseException:
        listener.close()
        raise
    return listener


def _serve(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="gremlin-town serve")
    parser.add_argument("--config", required=True)
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    arguments = parser.parse_args(argv)
    runtime = build_runtime(arguments.config)
    private_host = _loopback_override(arguments.host or runtime.settings.host)
    private_port = arguments.port or runtime.settings.port
    if runtime.public_app is None and runtime.mcp_app is None:
        uvicorn.run(
            runtime.app,
            host=private_host,
            port=private_port,
            workers=1,
            access_log=False,
            proxy_headers=False,
        )
        return 0

    public = runtime.public_gateway
    sockets: list[socket.socket] = []
    try:
        mcp = runtime.mcp
        ports = [private_port] + ([public.port] if public else []) + ([mcp.port] if mcp else [])
        if len(ports) != len(set(ports)):
            raise ValueError("listeners must use different ports")
        sockets.append(_listener(private_host, private_port))
        if public is not None:
            sockets.append(_listener(public.host, public.port))
        if mcp is not None:
            sockets.append(_listener(mcp.host, mcp.port))
        dispatch = ListenerDispatch(
            runtime.app,
            runtime.public_app,
            private_port=private_port,
            public_port=public.port if public else None,
            mcp_app=runtime.mcp_app,
            mcp_port=mcp.port if mcp else None,
        )
        server = uvicorn.Server(
            uvicorn.Config(dispatch, workers=1, access_log=False, lifespan="on", proxy_headers=False)
        )
        server.run(sockets=sockets)
    finally:
        for listener in sockets:
            listener.close()
        runtime.close()
    return 0


def _recovery(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="gremlin-town recovery")
    commands = parser.add_subparsers(dest="command", required=True)
    restore = commands.add_parser("import")
    restore.add_argument("--source", required=True, type=Path)
    restore.add_argument("--destination", required=True, type=Path)
    restore.add_argument("--hold-all-unfinished", action="store_true", required=True)
    audit = commands.add_parser("audit")
    audit.add_argument("--database", required=True, type=Path)
    try:
        arguments = parser.parse_args(argv)
        if arguments.command == "import":
            result = restore_snapshot(arguments.source, arguments.destination)
        else:
            result = audit_recovery(arguments.database)
        sys.stdout.write(json.dumps(asdict(result), default=str, sort_keys=True) + "\n")
        return 0
    except RecoveryError as exc:
        sys.stdout.write(
            json.dumps(
                {"error": {"code": exc.code.value, "message": exc.message}},
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2


def _brain_usage(argv: list[str]) -> int:
    """Print logged Brain Core calls as usage-log rows (or JSON) for operators."""
    import sqlite3

    from gremlin_town.mcp.brain_usage import read_usage, render_markdown

    parser = argparse.ArgumentParser(prog="gremlin-town brain-usage")
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--since", help="ISO date, e.g. 2026-09-25 (UTC)")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    try:
        arguments = parser.parse_args(argv)
        rows = read_usage(arguments.database, since=arguments.since)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    except (sqlite3.Error, ValueError) as exc:
        sys.stderr.write(f"brain-usage: cannot read log ({type(exc).__name__}: {exc})\n")
        return 2
    if arguments.format == "json":
        sys.stdout.write(json.dumps(rows, sort_keys=True) + "\n")
    else:
        sys.stdout.write(render_markdown(rows))
    return 0


def _triage(argv: list[str]) -> int:
    """Daily GT + Brain Core triage: run, show, weekly summary, mark a fix."""
    from gremlin_town import triage

    parser = argparse.ArgumentParser(prog="gremlin-town triage")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--config", required=True, type=Path)
    for name in ("run", "show", "weekly", "mark"):
        command = run if name == "run" else commands.add_parser(name)
        command.add_argument("--state-dir", required=True, type=Path)
    commands.choices["show"].add_argument("--format", choices=("text", "json"), default="text")
    mark = commands.choices["mark"]
    mark.add_argument("--key", required=True)
    choice = mark.add_mutually_exclusive_group(required=True)
    choice.add_argument("--shipped", help="commit, release or doc reference for the fix")
    choice.add_argument("--dismiss", action="store_true", help="not a real problem")
    mark.add_argument("--note", default="")
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2
    if arguments.command == "run":
        report = triage.run(arguments.config, arguments.state_dir)
        sys.stdout.write(triage.render_daily(report))
    elif arguments.command == "show":
        name = "latest.json" if arguments.format == "json" else "latest.txt"
        sys.stdout.write((arguments.state_dir / name).read_text(encoding="utf-8"))
    elif arguments.command == "weekly":
        sys.stdout.write(triage.weekly(arguments.state_dir))
    else:
        store = triage.IssueStore(arguments.state_dir / "triage.sqlite3")
        try:
            store.mark(arguments.key, shipped=arguments.shipped, dismiss=arguments.dismiss, note=arguments.note)
        except KeyError:
            sys.stderr.write(f"triage: no issue {arguments.key}\n")
            return 2
        finally:
            store.close()
        sys.stdout.write(f"marked {arguments.key}\n")
    return 0


def _review(argv: list[str]) -> int:
    """`[review]` (step E): the 06:00 review and right-moment return, off by default."""
    from datetime import UTC, datetime

    from gremlin_town.review.run import ReviewContext, run_moments, run_review

    parser = argparse.ArgumentParser(prog="gremlin-town review")
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run")
    run.add_argument("--config", required=True, type=Path)
    names = commands.add_parser("names")
    names.add_argument("--config", required=True, type=Path)
    moments = commands.add_parser("moments")
    moments.add_argument("--config", required=True, type=Path)
    moments.add_argument("--signal", required=True, choices=("pc_on", "task_finished", "calendar_gap"))
    moments.add_argument("--also-notify", help="the agent Nic is using at the time (D24)")
    moments.add_argument("--signal-id", help="a unique id for this occurrence, for replay-safety")
    poll = commands.add_parser("moments-poll")
    poll.add_argument("--config", required=True, type=Path)
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    runtime = build_runtime(arguments.config, start_worker=False)
    try:
        context = ReviewContext(
            settings=runtime.review,
            why_log=runtime.review_why_log,
            sources=runtime.review_sources,
            service=runtime.service,
            handoff_notes=runtime.review_handoff_notes,
            ranking=runtime.orientation.ranking,
            placement=runtime.placement_context,
            schedule_enabled=runtime.orientation_context.schedule_enabled,
            flowsavvy_enabled=runtime.flowsavvy.enabled,
            schedule=runtime.orientation_context.sources.schedule,
        )
        now = datetime.now(UTC)
        if arguments.command == "run":
            discord = getattr(runtime, "discord", None)
            if discord is not None and discord.enabled and discord.delivery:
                from dataclasses import replace
                from gremlin_town.review_card.delivery import RuntimeMorningSender
                context = replace(context, morning_sender=RuntimeMorningSender(runtime))
            result: Any = run_review(context, now)
        elif arguments.command == "names":
            from gremlin_town.review.names import run_names
            result = run_names(context, now)
        elif arguments.command == "moments":
            result = run_moments(
                context, now, arguments.signal,
                also_notify=arguments.also_notify, signal_id=arguments.signal_id,
                orientation=runtime.orientation_context,
            )
        else:
            from gremlin_town.review.poll import poll_once

            result = list(poll_once(context, now))
    finally:
        runtime.close()
    sys.stdout.write(json.dumps(result, sort_keys=True, default=str) + "\n")
    return 0


def _orient(argv: list[str]) -> int:
    """`[orientation]` (step F), off by default: "where am I?" (`overview`), "what next?"
    (`next`) and Nic's answer to a pick (`respond`)."""
    from datetime import UTC, datetime

    from brain_core.orientation import OrientationSignal
    from gremlin_town.review.orient import overview, respond, what_next

    parser = argparse.ArgumentParser(prog="gremlin-town orient")
    commands = parser.add_subparsers(dest="command", required=True)
    whole = commands.add_parser("overview", help='"where am I?": the whole picture, ranked')
    whole.add_argument("--config", required=True, type=Path)
    pick = commands.add_parser("next", help='"what next?": a top pick and two alternatives')
    pick.add_argument("--config", required=True, type=Path)
    pick.add_argument("--signal", choices=("pc_on", "task_finished", "calendar_gap"))
    pick.add_argument("--gap-minutes", type=int, help="only with --signal calendar_gap")
    answer = commands.add_parser("respond", help="Nic's yes, no, not_now or other to a pick")
    answer.add_argument("--config", required=True, type=Path)
    answer.add_argument("--actor", required=True, help="the agent relaying Nic's answer")
    answer.add_argument("--surfacing-id", required=True)
    answer.add_argument("--response", required=True, choices=("yes", "no", "not_now", "other"))
    answer.add_argument("--words", help="Nic's own words, for a correction or context")
    answer.add_argument("--target", help="an alternative's or can_wait record_id, only with --response other")
    answer.add_argument("--first-step", help="the suggested first step, or Nic's own words; the note starts with it")
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    runtime = build_runtime(arguments.config, start_worker=False)
    try:
        context = runtime.orientation_context
        now = datetime.now(UTC)
        if arguments.command == "overview":
            result = overview(context, now)
        elif arguments.command == "next":
            signal = None
            if arguments.signal is not None:
                signal = OrientationSignal(kind=arguments.signal, gap_minutes=arguments.gap_minutes)
            result = what_next(context, now, signal=signal)
        else:
            result = respond(
                context, now, actor=arguments.actor, surfacing_id=arguments.surfacing_id,
                response=arguments.response, words=arguments.words, target=arguments.target,
                first_step=arguments.first_step,
            )
    finally:
        runtime.close()
    sys.stdout.write(json.dumps(result, sort_keys=True, default=str) + "\n")
    return 0


def _notion_token(settings: Any) -> str:
    """The Notion key: systemd's credential (`GT_NOTION_TOKEN_FILE`) first, else `[notion_view] token_file`."""
    import os

    path = os.environ.get("GT_NOTION_TOKEN_FILE") or settings.token_file
    if not path:
        raise SystemExit("notion-view: no Notion key (set GT_NOTION_TOKEN_FILE or [notion_view] token_file)")
    return Path(path).read_text(encoding="utf-8").strip()


def _notion_view(argv: list[str]) -> int:
    """`[notion_view]` (step H), off by default: Brain Home's items copied one way to a Notion database."""
    import logging
    from datetime import UTC, datetime

    from brain_core.timewords import today_in_singapore

    from gremlin_town.notion_view.client import NotionClient
    from gremlin_town.notion_view.mapping import picked_today_refs
    from gremlin_town.notion_view.settings import NotionViewSettings
    from gremlin_town.notion_view.state import ViewState
    from gremlin_town.notion_view.sync import status, sync_once, watch
    from gremlin_town.review.why_log import WhyLog
    from gremlin_town.runtime import _cached_brain_storage, _read_toml

    parser = argparse.ArgumentParser(prog="gremlin-town notion-view")
    commands = parser.add_subparsers(dest="command", required=True)
    once = commands.add_parser("sync", help="copy once and exit")
    once.add_argument("--force", action="store_true", help="resend every row and archive rows it no longer tracks")
    once.add_argument("--dry-run", action="store_true", help="read Brain Home and print what would change; no Notion key")
    commands.add_parser("watch", help="check Brain Home every check_seconds and copy what changed")
    commands.add_parser("status", help="the last copy, the last error and the row count")
    for command in commands.choices.values():
        command.add_argument("--config", required=True, type=Path)
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    settings = NotionViewSettings.from_table(_read_toml(arguments.config).get("notion_view"), base=arguments.config.parent)
    if not settings.enabled or settings.state_file is None:
        sys.stderr.write("notion-view: [notion_view] is not enabled\n")
        return 2
    state = ViewState(settings.state_file)
    why_log: WhyLog | None = None
    copy_store: Any = None
    try:
        if arguments.command == "status":
            sys.stdout.write(json.dumps(status(state), sort_keys=True) + "\n")
            return 0
        runtime = build_runtime(arguments.config, start_worker=False)
        try:
            brain, review = runtime.brain, runtime.review
        finally:
            runtime.close()
        if brain is None:
            sys.stderr.write("notion-view: needs [brain_core] for Brain Home\n")
            return 2
        # Through the hub's copy of Brain Home, the same file the service uses, so a restart doesn't
        # download every item again, and within the hub's limits (the storage-limit plan, layer 1).
        storage, copy_store = _cached_brain_storage(brain)
        if review is not None and review.enabled and review.why_log_file:
            why_log = WhyLog(review.why_log_file)

        def picked_today(now: datetime) -> frozenset[str]:
            return frozenset() if why_log is None else picked_today_refs(why_log.history(), today_in_singapore(now))

        dry_run = arguments.command == "sync" and arguments.dry_run
        client = None if dry_run else NotionClient(_notion_token(settings))
        if arguments.command == "watch":
            if client is None or not settings.database_id:
                sys.stderr.write("notion-view: watch needs the Notion key and [notion_view] database_id\n")
                return 2
            logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
            watch(
                storage=storage, client=client, state=state, database_id=settings.database_id,
                done_days=settings.done_days, repair_hours=settings.repair_hours,
                check_seconds=settings.check_seconds, picked_today=picked_today,
            )
            return 0
        now = datetime.now(UTC)
        result = sync_once(
            storage=storage, client=client, state=state, database_id=settings.database_id, now=now,
            done_days=settings.done_days, picked=picked_today(now), force=arguments.force, sweep=arguments.force,
            dry_run=dry_run,
        )
        sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
        return 0
    finally:
        if why_log is not None:
            why_log.close()
        if copy_store is not None:
            copy_store.close()
        state.close()


BRAIN_HOME_SIZE_EXIT = {"ok": 0, "heads_up": 1, "urgent": 2, "over": 2}
BRAIN_HOME_UNCOUNTED_EXIT = 3
# Not 1 or 2, which mean a heads-up and urgent: argparse's own 2 is replaced.
BRAIN_HOME_USAGE_EXIT = 4


def _brain_home_size_report(counts: Any, allowance: Any) -> dict[str, Any]:
    from datetime import UTC, datetime

    from brain_core.capacity import headroom, level_rank

    from gremlin_town.review.brain_source import HUB_SCANS

    entries = headroom(counts, HUB_SCANS)
    worst = max(entries, key=lambda entry: level_rank(entry.level))
    rows = [
        {
            "name": entry.name, "kinds": list(entry.kinds), "files": entry.files, "max_files": entry.max_files,
            "bytes": entry.bytes, "max_bytes": entry.max_bytes, "share": round(entry.share, 4),
            "level": entry.level, "sentence": entry.sentence,
        }
        for entry in entries[:-1]
    ]
    tree = entries[-1]
    if isinstance(allowance, dict):
        hourly = allowance
    else:
        hourly = {
            "limit": allowance.limit, "remaining": allowance.remaining, "used": allowance.used,
            "resets_at": datetime.fromtimestamp(allowance.reset, UTC).isoformat(),
        }
    return {
        "head": counts.revision.commit_sha[:12],
        "level": worst.level,
        "scans": rows,
        "tree": {"entries": tree.files, "limit": tree.max_files, "level": tree.level, "sentence": tree.sentence},
        "live_files": dict(counts.live_files),
        "live_bytes": dict(counts.live_bytes),
        "archived_files": dict(counts.archived_files),
        "receipts": counts.receipts,
        "other_files": counts.other_files,
        "hourly_allowance": hourly,
    }


def _brain_home_size_text(report: dict[str, Any]) -> str:
    from brain_core.capacity import megabytes

    words = {"ok": "fine", "heads_up": "heads-up", "urgent": "urgent", "over": "over a limit"}
    lines = [f"Brain Home at {report['head']}: {words[report['level']]}"]
    for row in report["scans"]:
        size = f", {megabytes(row['bytes'])} of {megabytes(row['max_bytes'])}" if row["max_bytes"] else ""
        lines.append(
            f"  {row['name']}: {row['files']:,} of {row['max_files']:,} files{size} ({row['share']:.0%}) - "
            f"{words[row['level']]}"
        )
    tree = report["tree"]
    lines.append(f"  file list: {tree['entries']:,} entries of about {tree['limit']:,} - {words[tree['level']]}")
    archived = ", ".join(f"{kind} {count:,}" for kind, count in report["archived_files"].items()) or "none"
    lines.append(f"Archived: {archived}. Receipts: {report['receipts']:,}. Other files: {report['other_files']:,}.")
    hourly = report["hourly_allowance"]
    if "error" in hourly:
        lines.append(f"GitHub hourly allowance: couldn't read it ({hourly['error']})")
    else:
        lines.append(
            f"GitHub hourly allowance: {hourly['remaining']:,} of {hourly['limit']:,} left "
            f"({hourly['used']:,} used), refills {hourly['resets_at']}"
        )
    for row in [*report["scans"], tree]:
        if row["level"] != "ok":
            lines.append(f"- {row['sentence']}")
    return "\n".join(lines) + "\n"


def _brain_home(argv: list[str]) -> int:
    """`gremlin-town brain-home size`: Brain Home's size against every scan the hub runs, read-only
    (docs/product/2026-09-30-brain-home-storage-limit-plan.md, section 5). Four GitHub reads - the
    branch, the commit, the tree and the hourly allowance - and nothing written to Brain Home or
    GitHub. Exit 0 when all is fine, 1 at a heads-up (50% of a limit), 2 when urgent (80%) or over,
    3 when Brain Home couldn't be counted (the config, the credential or GitHub failed), 4 for a usage
    error. Every failure says why on stderr, never a bare traceback whose exit code would read as a level."""

    from gremlin_town.review.brain_source import describe_failure
    from gremlin_town.runtime import _orientation_brain_storage

    parser = argparse.ArgumentParser(prog="gremlin-town brain-home")
    commands = parser.add_subparsers(dest="command", required=True)
    size = commands.add_parser("size", help="Brain Home's size against the hub's read limits; read-only")
    size.add_argument("--config", required=True, type=Path)
    size.add_argument("--format", choices=("text", "json"), default="text")
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code == 0 else BRAIN_HOME_USAGE_EXIT  # --help is 0

    try:
        runtime = build_runtime(arguments.config, start_worker=False)
        try:
            brain = runtime.brain
        finally:
            runtime.close()
    except Exception as exc:  # a bad config or a missing file: said plainly, with its own exit code
        sys.stderr.write(f"brain-home: couldn't read the config: {type(exc).__name__}: {exc}\n")
        return BRAIN_HOME_UNCOUNTED_EXIT
    if brain is None:
        sys.stderr.write("brain-home: needs [brain_core] for Brain Home\n")
        return BRAIN_HOME_UNCOUNTED_EXIT
    try:
        # The plain storage: counting reads the tree and no file, so the hub's copy isn't needed.
        storage = _orientation_brain_storage(brain)
    except Exception as exc:  # the GitHub credential couldn't be loaded
        sys.stderr.write(f"brain-home: couldn't load the Brain Home credential: {type(exc).__name__}: {exc}\n")
        return BRAIN_HOME_UNCOUNTED_EXIT
    try:
        counts = storage.record_counts()
    except Exception as exc:  # reported, with its exit code; nothing is guessed
        sys.stderr.write(f"brain-home: couldn't count Brain Home: {describe_failure(exc)}\n")
        return BRAIN_HOME_UNCOUNTED_EXIT
    try:
        allowance: Any = storage.rate_limit()
    except Exception as exc:  # the counts still stand; the allowance line says why it's missing
        allowance = {"error": describe_failure(exc)}
    report = _brain_home_size_report(counts, allowance)
    if arguments.format == "json":
        sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    else:
        sys.stdout.write(_brain_home_size_text(report))
    return BRAIN_HOME_SIZE_EXIT[report["level"]]


def _placement(argv: list[str]) -> int:
    """`[placement]` (step I, build plan slice 4): the day's plan, the last run, or a rollback."""
    from datetime import UTC, datetime

    from gremlin_town.review import placement

    parser = argparse.ArgumentParser(prog="gremlin-town placement")
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="plan today now; with --dry-run, read only and change nothing")
    plan.add_argument("--config", required=True, type=Path)
    plan.add_argument("--dry-run", action="store_true")
    shown = commands.add_parser("status", help="the last run, its outcome and its error")
    shown.add_argument("--config", required=True, type=Path)
    clear = commands.add_parser("clear", help="remove today's future engine entries (a rollback; Nic's go)")
    clear.add_argument("--config", required=True, type=Path)
    clear.add_argument("--future", action="store_true", required=True)
    try:
        arguments = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code) if isinstance(exc.code, int) else 2

    runtime = build_runtime(arguments.config, start_worker=False)
    try:
        context = runtime.placement_context
        if context is None:
            sys.stderr.write("placement: [placement] is not enabled\n")
            return 2
        now = datetime.now(UTC)
        if arguments.command == "plan":
            result: Any = placement.run_placement(context, now, dry_run=arguments.dry_run)
        elif arguments.command == "status":
            result = placement.status(context.why_log)
        else:
            if context.writer is None:
                sys.stderr.write("placement: no calendar writer (Google calendar.events)\n")
                return 2
            result = {"removed": placement.clear_future(context.writer, now)}
    finally:
        runtime.close()
    sys.stdout.write(json.dumps(result, sort_keys=True, default=str) + "\n")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    values = list(argv) if argv is not None else sys.argv[1:]
    if values and values[0] == "discord":
        from gremlin_town.review_card.run import main as discord_main
        return discord_main(values[1:])
    if values and values[0] == "dashboard":
        from gremlin_town.dashboard.run import main as dashboard_main
        return dashboard_main(values[1:])
    if values and values[0] == "flowsavvy":
        from gremlin_town.flowsavvy.run import main as flowsavvy_main
        return flowsavvy_main(values[1:])
    if values and values[0] == "notion-view":
        return _notion_view(values[1:])
    if values and values[0] == "serve":
        return _serve(values[1:])
    if values and values[0] == "recovery":
        return _recovery(values[1:])
    if values and values[0] == "brain-usage":
        return _brain_usage(values[1:])
    if values and values[0] == "triage":
        return _triage(values[1:])
    if len(values) >= 2 and values[0] == "review" and values[1] in ("run", "names", "moments", "moments-poll"):
        # Local, config-driven server-side runs. "review respond" is a remote
        # client action (like "moments signal") and falls through to client_main.
        return _review(values[1:])
    if values and values[0] == "orient":
        return _orient(values[1:])
    if values and values[0] == "placement":
        return _placement(values[1:])
    if values and values[0] == "brain-home":
        return _brain_home(values[1:])
    return client_main(values)
