"""Thin live runner: the recall benchmark's 20 questions against a real hub,

read-only, through the same ``memory.search`` operation and JSON hub client
the ``gremlin-town`` CLI already uses (``gremlin_town.cli.client.JsonApiClient``).
Makes ``memory.search`` calls only - no save, no change, no decision.

Not wired into ``gremlin_town.cli.main``'s argparse tree: this is deliberately
a small, separate script (the goal left the choice open) so it stays reachable
without touching that larger, already heavily-tested dispatcher. Promoting it
to a first-class ``gremlin-town brain benchmark recall`` subcommand is a later,
Nic's-call step - see the recall benchmark stream's report.

Usage (never run in CI; unit-tested in tests/benchmarks/test_recall_cli.py
with a fake transport instead)::

    python -m gremlin_town.benchmarks.recall_cli \\
        --base-url https://hub.example --token "$GREMLIN_TOWN_TOKEN" \\
        --questions components/gremlin-town/tests/benchmarks/recall_fixtures/questions.json \\
        --scope personal
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path

from gremlin_town.cli.client import CliError, JsonApiClient

from .recall import RecallReport, load_questions, run


def hub_search(
    client: JsonApiClient,
    *,
    scope: str = "personal",
    project_id: str | None = None,
):
    """A recall.SearchFn bound to one hosted hub's POST /v1/brain/memory.search.

    Same request shape as ``gremlin-town brain memory.search --json '...'``
    (see ``gremlin_town.mcp.brain_tools.bc_memory_search``): ``query``,
    ``scope``, ``limit``, and ``project_id`` only when scoped to a project.
    """

    def search(query: str, limit: int):
        payload: dict[str, object] = {"query": query, "scope": scope, "limit": limit}
        if project_id is not None:
            payload["project_id"] = project_id
        result = client.request("POST", "/v1/brain/memory.search", payload=payload)
        matches = result.get("matches", []) if isinstance(result, dict) else []
        return tuple(str(match["record_id"]) for match in matches)

    return search


def _report_to_json(report: RecallReport) -> dict[str, object]:
    # dataclasses.asdict recurses through the outcomes tuple's own QuestionOutcome
    # dataclasses (and turns every tuple into a list) in one pass - no manual walk needed.
    return asdict(report)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gremlin-town-recall",
        description="Run the Brain search recall benchmark's questions against a live hub, read-only.",
    )
    parser.add_argument("--base-url", required=True, help="hub base URL (no default: never point this at a hub by accident)")
    parser.add_argument("--token", default=None, help="bearer token (or GREMLIN_TOWN_TOKEN)")
    parser.add_argument("--questions", required=True, type=Path, help="questions JSON file (see recall_fixtures/questions.json)")
    parser.add_argument("--scope", default="personal", choices=("personal", "cross-project", "project"))
    parser.add_argument("--project-id", default=None, help="required when --scope is project")
    parser.add_argument("--limit", type=int, default=5, help="k for recall@k (default 5, matching P6)")
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser


def main(argv: Sequence[str] | None = None, *, client: JsonApiClient | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else sys.argv[1:])
    try:
        if args.scope == "project" and not args.project_id:
            raise CliError("--project-id is required when --scope is project", code="INVALID_INPUT", status_code=2)
        questions = load_questions(args.questions)
        api_client = client
        if api_client is None:
            token = args.token if args.token is not None else os.environ.get("GREMLIN_TOWN_TOKEN")
            if not token or not token.strip():
                raise CliError("a bearer token is required (--token or GREMLIN_TOWN_TOKEN)", code="UNAUTHORIZED", status_code=2)
            api_client = JsonApiClient(args.base_url, token.strip(), timeout=args.timeout)
        search = hub_search(api_client, scope=args.scope, project_id=args.project_id)
        report = run(search, questions, k=args.limit)
        sys.stdout.write(json.dumps(_report_to_json(report), ensure_ascii=True, sort_keys=True) + "\n")
        return 0
    except CliError as exc:
        error = {"error": {"code": exc.code, "message": str(exc)}}
        sys.stdout.write(json.dumps(error, ensure_ascii=True, sort_keys=True) + "\n")
        return exc.status_code


if __name__ == "__main__":
    raise SystemExit(main())
