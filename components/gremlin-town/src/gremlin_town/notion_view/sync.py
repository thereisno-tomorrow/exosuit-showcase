"""Copying Brain Home to the Notion view, one way (docs/product/steps/h-notion-view.md §1, §2, §4).

`sync_once` makes Notion match one Brain Home revision; `watch` checks the branch head every `check_seconds` and
copies only when it, or today's picks, changed, or the daily repair is due. Nothing here writes to Brain Home.

Items are read within the hub's limits, 5,000 files or 32 MiB a scan, through the hub's copy of Brain Home
(`entrypoint.py`; docs/product/2026-09-30-brain-home-storage-limit-plan.md, layer 1).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Protocol

from gremlin_town.mcp.brain_home_copy import SCAN_MAX_BYTES, SCAN_MAX_RECORDS
from gremlin_town.notion_view.client import NotionClient, NotionError
from gremlin_town.notion_view.mapping import content_hash, plan_rows, properties, schema_problems
from gremlin_town.notion_view.state import ViewState

logger = logging.getLogger(__name__)

_ITEM_KIND = frozenset({"item"})
LAST_COMMIT = "last_commit"
LAST_PICKED = "last_picked"
LAST_SUCCESS = "last_success"
LAST_ERROR = "last_error"
LAST_REPAIR = "last_repair"


class NotionViewError(RuntimeError):
    """The view can't be copied as configured (for example, the database is missing a property)."""


class BrainSource(Protocol):
    """What `GitHubBrainHomeStorage` offers that the copy needs."""

    def revision(self) -> Any: ...

    def snapshot_records(
        self, *, kinds: frozenset[str], revision: Any = None, max_records: int = ..., max_bytes: int = ...,
    ) -> Any: ...


def _picked_key(picked: frozenset[str]) -> str:
    return json.dumps(sorted(picked))


def check_schema(client: NotionClient, database_id: str) -> None:
    found = client.database(database_id).get("properties")
    problems = schema_problems(found if isinstance(found, dict) else {})
    if problems:
        raise NotionViewError("the Notion database doesn't match the view: " + "; ".join(problems))


def _brain_id(page: dict[str, Any]) -> str:
    value = (page.get("properties") or {}).get("Brain ID") or {}
    return "".join(part.get("plain_text", "") for part in value.get("rich_text") or () if isinstance(part, dict))


def sync_once(
    *,
    storage: BrainSource,
    client: NotionClient | None,
    state: ViewState,
    database_id: str | None,
    now: datetime,
    done_days: int,
    picked: frozenset[str] = frozenset(),
    force: bool = False,
    sweep: bool = False,
    dry_run: bool = False,
    revision: Any = None,
) -> dict[str, Any]:
    """Make the Notion database match Brain Home at one revision; returns counts of what was done.

    ``force`` resends every row; ``sweep`` also archives rows this view made that it no longer tracks (left by a
    create that failed after Notion made the page, or a lost state file). Rows without a Brain ID, typed in Notion
    by hand, are never touched. ``dry_run`` needs no client and writes nothing, not even to the state file."""

    revision = storage.revision() if revision is None else revision
    read = storage.snapshot_records(
        kinds=_ITEM_KIND, revision=revision, max_records=SCAN_MAX_RECORDS, max_bytes=SCAN_MAX_BYTES,
    )
    rows = plan_rows((file.record for file in read.records), now=now, done_days=done_days, picked=picked)
    known = state.rows()
    stale = sorted(set(known) - {row.root_id for row in rows})

    if dry_run:
        page_for = {root: page for root, (page, _) in known.items()}
        changed = sum(
            1 for row in rows
            if row.root_id in known and (force or content_hash(properties(row, page_for=page_for)) != known[row.root_id][1])
        )
        return {
            "dry_run": True, "commit": revision.commit_sha, "rows": len(rows),
            "create": sum(1 for row in rows if row.root_id not in known), "update": changed, "archive": len(stale),
        }

    if client is None or not database_id:
        raise NotionViewError("a copy needs the Notion key and [notion_view] database_id")
    try:
        check_schema(client, database_id)
        created = updated = archived = swept = 0
        for row in rows:
            if row.root_id not in known:
                values = properties(row, page_for={})
                page_id = client.create_page(database_id, values)
                state.put(row.root_id, page_id, content_hash(values))
                created += 1
        current = state.rows()
        page_for = {root: page for root, (page, _) in current.items()}
        for row in rows:
            page_id, sent = current[row.root_id]
            values = properties(row, page_for=page_for)
            digest = content_hash(values)
            if digest != sent or (force and row.root_id in known):
                client.update_page(page_id, values)
                state.put(row.root_id, page_id, digest)
                updated += 1
        for root in stale:
            client.archive_page(known[root][0])
            state.remove(root)
            archived += 1
        if sweep:
            tracked_pages = {page for page, _ in state.rows().values()}
            for page in client.pages(database_id):
                page_id = page.get("id")
                if page.get("archived") or not isinstance(page_id, str) or page_id in tracked_pages:
                    continue
                if _brain_id(page):
                    client.archive_page(page_id)
                    swept += 1
    except (NotionError, NotionViewError) as exc:
        state.set(LAST_ERROR, f"{now.isoformat()} {exc}")
        raise
    state.set(LAST_COMMIT, revision.commit_sha)
    state.set(LAST_PICKED, _picked_key(picked))
    state.set(LAST_SUCCESS, now.isoformat())
    state.set(LAST_ERROR, None)
    return {
        "dry_run": False, "commit": revision.commit_sha, "rows": len(rows),
        "created": created, "updated": updated, "archived": archived, "swept": swept,
    }


def status(state: ViewState) -> dict[str, Any]:
    return {
        "rows": len(state.rows()),
        "last_commit": state.get(LAST_COMMIT),
        "last_success": state.get(LAST_SUCCESS),
        "last_repair": state.get(LAST_REPAIR),
        "last_error": state.get(LAST_ERROR),
    }


def watch_once(
    *,
    storage: BrainSource,
    client: NotionClient,
    state: ViewState,
    database_id: str,
    now: datetime,
    done_days: int,
    repair_hours: int,
    picked: frozenset[str],
) -> dict[str, Any] | None:
    """One check: copy if the head or today's picks moved, or the repair is due; otherwise ``None``."""

    revision = storage.revision()
    last_repair = state.get(LAST_REPAIR)
    repair_due = last_repair is None or datetime.fromisoformat(last_repair) <= now - timedelta(hours=repair_hours)
    if not repair_due and revision.commit_sha == state.get(LAST_COMMIT) and _picked_key(picked) == state.get(LAST_PICKED):
        return None
    result = sync_once(
        storage=storage, client=client, state=state, database_id=database_id, now=now, done_days=done_days,
        picked=picked, force=repair_due, sweep=repair_due, revision=revision,
    )
    if repair_due:
        state.set(LAST_REPAIR, now.isoformat())
    return result


def watch(
    *,
    storage: BrainSource,
    client: NotionClient,
    state: ViewState,
    database_id: str,
    done_days: int,
    repair_hours: int,
    check_seconds: int,
    picked_today: Callable[[datetime], frozenset[str]],
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
    keep_going: Callable[[], bool] = lambda: True,
) -> None:
    """Check every ``check_seconds`` until ``keep_going`` says stop. A failed check is logged as an error and
    recorded in the state file (`status` shows it), and the next check tries again."""

    while keep_going():
        now = clock()
        try:
            result = watch_once(
                storage=storage, client=client, state=state, database_id=database_id, now=now,
                done_days=done_days, repair_hours=repair_hours, picked=picked_today(now),
            )
            if result is not None:
                logger.info("notion view copied: %s", json.dumps(result, sort_keys=True))
        except Exception as exc:  # logged loudly and kept in the state file; the service keeps checking
            from gremlin_town.review.brain_source import describe_failure

            # A Brain Home size limit says so plainly (the storage-limit plan, layer 1).
            state.set(LAST_ERROR, f"{now.isoformat()} {describe_failure(exc)}")
            logger.error("notion view copy failed: %s", describe_failure(exc))
        sleep(check_seconds)
