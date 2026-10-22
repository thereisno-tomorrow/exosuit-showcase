from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_process_crash_recovers_incremental_continuity(tmp_path: Path) -> None:
    writer = r'''
import hashlib
import os
import sys
from datetime import UTC, datetime, timedelta
from brain_core.continuity import append_event, start_continuity
from brain_core.records import Evidence, RecordStore

root = sys.argv[1]
store = RecordStore(root)
start = datetime(2026, 8, 29, 3, 0, 0, tzinfo=UTC)

def evidence(index):
    locator = f"session:crash:{index}"
    return Evidence(
        source_type="session_message",
        locator=locator,
        content_sha256=hashlib.sha256(locator.encode()).hexdigest(),
        excerpt=locator,
        session_id="crash-session",
        actor="nic",
    )

start_continuity(
    store,
    session_id="crash-session",
    intent="Prove continuity survives a hard process exit",
    sources=["accepted Phase 2 package"],
    evidence=evidence(0),
    now=start,
    next_step="Record meaningful events",
    entropy="a1b2",
)
events = [
    ("effect", "Record core written"),
    ("decision", "Phase 2 approved"),
    ("candidate", "Possible later adapter"),
    ("blocker", "B2 credentials missing"),
    ("next_step", "Complete the remote backup gate"),
]
for index, (event, value) in enumerate(events, start=1):
    append_event(
        store,
        session_id="crash-session",
        event=event,
        value=value,
        evidence=evidence(index),
        now=start + timedelta(seconds=index),
        entropy=f"{index + 1:04x}",
    )
os._exit(23)
'''
    crashed = subprocess.run(
        [sys.executable, "-c", writer, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert crashed.returncode == 23
    assert crashed.stdout == crashed.stderr == ""

    request = {
        "protocol_version": "1",
        "request_id": "recover-after-crash",
        "operation": "continuity.recover",
        "parameters": {"brain_home": str(tmp_path), "max_tokens": 2_500},
    }
    recovered = subprocess.run(
        [sys.executable, "-m", "brain_core", "json"],
        input=json.dumps(request),
        check=True,
        capture_output=True,
        text=True,
    )
    envelope = json.loads(recovered.stdout)
    brief = envelope["data"]
    assert brief["session_id"] == "crash-session"
    assert brief["sources"] == ["accepted Phase 2 package"]
    assert brief["effects"] == ["Record core written"]
    assert brief["decisions"] == ["Phase 2 approved"]
    assert brief["candidates"] == ["Possible later adapter"]
    assert brief["blockers"] == ["B2 credentials missing"]
    assert brief["next_step"] == "Complete the remote backup gate"
    assert brief["evidence"][-1]["locator"] == "session:crash:5"
    assert len(brief["evidence"][-1]["content_sha256"]) == 64
    assert brief["tokens"] <= 2_500
