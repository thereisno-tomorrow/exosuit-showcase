"""Hub command and daemon: immediate durable wakes plus periodic reconciliation."""
import argparse
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import sys
import time

from .client import FlowSavvyClient, FlowSavvyError
from .settings import FlowSavvySettings
from .snapshot import read_snapshot
from .state import SyncState
from .sync import sync_once


def watch(*, settings, state, reconcile, sleep=time.sleep, clock=time.monotonic, stop=lambda: False):
    """Wakes contain no item IDs/content. Read Brain Home fresh on every run.

    Fail loudly to the service manager on any surprise; no mutation is retried
    inside this loop. The manager leaves a failed service stopped for inspection.
    """
    if not settings.enabled:
        return
    handled = None
    next_check = 0.0
    while not stop():
        version = state.wake_version()
        if version != handled or clock() >= next_check:
            reconcile()
            handled = version  # A wake during sync is observed next iteration.
            next_check = clock() + settings.check_seconds
        sleep(1)


def main(argv):
    parser = argparse.ArgumentParser(prog="gremlin-town flowsavvy")
    parser.add_argument("command", choices=("sync", "watch", "status"))
    parser.add_argument("--config", required=True, type=Path)
    args = parser.parse_args(argv)
    from gremlin_town.runtime import _cached_brain_storage, _read_toml, build_runtime
    state = client = runtime = copy_store = None
    try:
        settings = FlowSavvySettings.from_table(_read_toml(args.config).get("flowsavvy"), base=args.config.parent)
        if not settings.enabled:
            print(json.dumps({"enabled": False}))
            return 0
        if args.command == "status":
            snapshot = read_snapshot(settings.state_file, datetime.now(UTC), settings.max_age_seconds)
            print(json.dumps({"enabled": True, "available": snapshot.available,
                              "age_seconds": snapshot.age_seconds, "message": snapshot.message}))
            return 0
        runtime = build_runtime(args.config, start_worker=False)
        if runtime.brain is None:
            raise FlowSavvyError("FlowSavvy needs the hub's Brain Home reader")
        # Direct cached read adapter; no writer API is exposed to this connector.
        storage, copy_store = _cached_brain_storage(runtime.brain)
        from gremlin_town.review.brain_source import brain_items_reader
        from gremlin_town.mcp.brain_home_copy import SCAN_MAX_BYTES, SCAN_MAX_RECORDS
        from brain_core.timewords import today_in_singapore
        reader = brain_items_reader(storage, max_records=SCAN_MAX_RECORDS, max_bytes=SCAN_MAX_BYTES)
        credential = Path(os.environ.get("GT_FLOWSAVVY_TOKEN_FILE") or settings.token_file)
        client = FlowSavvyClient(credential.read_text(encoding="utf-8").strip())
        state = SyncState(settings.state_file)

        def reconcile():
            now = datetime.now(UTC)
            try:
                records = tuple(row.record for row in reader())
                history = runtime.review_why_log.history() if runtime.review_why_log else ()
                picked = frozenset(row.item_ref for row in history if row.response == "picked_today"
                                   and today_in_singapore(row.responded_at or row.surfaced_at) == today_in_singapore(now))
            except Exception:
                state.set("error", "brain_read_failed")
                raise FlowSavvyError("FlowSavvy couldn't read Brain Home; no task changes were sent") from None
            return sync_once(records=records, client=client, state=state, settings=settings, now=now, picked=picked)

        if args.command == "watch":
            watch(settings=settings, state=state, reconcile=reconcile)
        else:
            print(json.dumps(reconcile(), sort_keys=True))  # counts only
        return 0
    except FlowSavvyError as exc:
        sys.stderr.write(f"flowsavvy: {exc}\n")
        return 2
    except Exception:
        # Config, credentials and storage errors can contain private paths/values.
        sys.stderr.write("flowsavvy: configuration, credential or local state could not be read\n")
        return 2
    finally:
        for resource in (client, state, copy_store, runtime):
            if resource is not None:
                resource.close()
