"""Prepare our scheduled reset request; never manipulate Hermes sessions.

The supported gateway consumer is a release blocker. This trigger alone is NOT
a functioning daily reset. No option is selected by this script.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


def reset_day(now: datetime) -> str:
    local = now.astimezone(timezone(timedelta(hours=8)))
    return (local - timedelta(hours=4)).date().isoformat()


def request(home: Path, now: datetime) -> str:
    folder = home / "reset-request"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "pending.json"
    day = reset_day(now)
    if path.exists() and json.loads(path.read_text(encoding="utf-8"))["day"] == day:
        return "already requested; gateway consumer still required"
    temp = folder / "pending.tmp"
    temp.write_text(json.dumps({"day": day, "not_before": "04:00 Asia/Singapore",
                               "state": "awaiting supported consumer"}) + "\n", encoding="utf-8")
    temp.replace(path)
    return "requested; gateway consumer still required"


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--request", action="store_true", required=True)
    p.add_argument("--home", type=Path, required=True)
    args = p.parse_args()
    print(request(args.home, datetime.now(timezone.utc)))
