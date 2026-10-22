"""P3: a memory save's pinned time belongs to its authenticated caller.

Two callers that pick the same operation id each keep their own first time: their
record's created_at, the date their time words resolve against, and the record a
replay returns, even when a lost reply makes one of them retry after the clock
moved. Decisions and continuity keep the plain per-operation time.
"""

from __future__ import annotations

import json
import hashlib
from datetime import datetime
from pathlib import Path

import anyio
import pytest

from brain_core.records import parse_record
from gremlin_town.mcp.brain_assembly import build_brain_runtime
from gremlin_town.mcp.brain_runtime import (
    BrainCaller,
    BrainOperation as Op,
    BrainRuntimeError,
    MemorySaveRequest,
    OwnerSelector,
    WorkRequest,
)
from gremlin_town.mcp.github_brain_provider import BrainCoreRecordAdapter
from gremlin_town.mcp.host_identity import bind_write_attribution
from test_brain_composition import _settings
from test_github_brain_provider import (
    FIRST_TIME,
    LANE_B,
    _authorization,
    _decision,
    _evidence,
    _memory_change_request,
    _memory_request,
    _provider,
    _HistoryTransport,
)

MONDAY = datetime.fromisoformat("2026-09-21T12:00:00+08:00")
THURSDAY = datetime.fromisoformat("2026-09-24T09:30:00+08:00")
SATURDAY = datetime.fromisoformat("2026-09-26T21:00:00+08:00")
CALLERS = ("chatgpt-web", "claude-web")


def _allow_both_callers(policy_file: Path) -> None:
    policy = json.loads(policy_file.read_text(encoding="utf-8"))
    owner = policy["owners"][0]
    for field in ("readers", "writers"):
        owner[field] = sorted(set(owner[field]) | set(CALLERS))
    policy_file.write_text(json.dumps(policy), encoding="utf-8")


def _request() -> MemorySaveRequest:
    return MemorySaveRequest(
        operation_id="call-the-bank",
        kind="item",
        title="Call the bank",
        body="Call the bank tomorrow.",
        source_reference="mcp-request:call-the-bank",
        said_by="owner",
        came_by="direct",
        words="Call the bank tomorrow.",
        level="accepted",
        time_words="tomorrow",
    )


def test_two_callers_with_one_operation_id_each_keep_their_own_first_time(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    _allow_both_callers(settings.policy_file)
    remote = LANE_B.FakeGitHub()
    now = [MONDAY]

    def save_as(subject: str):
        async def run():
            # A fresh runtime each time, as after a restart: the pinned times live in
            # the provider state on disk, not in memory.
            runtime = build_brain_runtime(settings, github_transport=_HistoryTransport(remote), operation_clock=lambda: now[0])
            try:
                with bind_write_attribution(subject):
                    return await runtime.save_memory(
                        BrainCaller(subject, frozenset({"bc:read", "bc:write"})), OwnerSelector("personal"), _request()
                    )
            finally:
                await runtime.aclose()

        return anyio.run(run)

    first = save_as("chatgpt-web")

    now[0] = THURSDAY
    remote.lost_reject_count = 2
    with pytest.raises(BrainRuntimeError) as lost:
        save_as("claude-web")
    assert lost.value.code == "brain.record.write_failed" and lost.value.retryable
    assert remote.lost_reject_count==1
    # Atomic batch publication reports each lost unaccepted reply honestly;
    # the second rejection is a separate caller retry, still at its first instant.
    with pytest.raises(BrainRuntimeError) as lost_again:
        save_as('claude-web')
    assert lost_again.value.code=='brain.record.write_failed' and lost_again.value.retryable
    assert remote.lost_reject_count==0

    now[0] = SATURDAY
    second = save_as("claude-web")
    replays = {subject: save_as(subject) for subject in CALLERS}

    assert first.status == second.status == "accepted"
    assert first.record_id != second.record_id
    assert first.timing==second.timing=={}
    assert replays["chatgpt-web"].status == replays["claude-web"].status == "duplicate"
    assert replays["chatgpt-web"].record_id == first.record_id
    assert replays["claude-web"].record_id == second.record_id

    tree = remote.trees[remote.commits[remote.branch]["tree"]]
    records = {
        record.source["recorded_by"]: record
        for record in (
            parse_record(remote.blobs[blob].decode("utf-8"), path)
            for path, blob in tree.items()
            if path.startswith("records/item/")
        )
    }
    assert set(records) == set(CALLERS)
    assert records["chatgpt-web"].id == first.record_id and records["chatgpt-web"].created_at == MONDAY
    assert records["claude-web"].id == second.record_id and records["claude-web"].created_at == THURSDAY
    for subject,day,read_on in (('chatgpt-web','2026-09-22','2026-09-21'),('claude-web','2026-09-25','2026-09-24')):
        assert records[subject].data['dates']['planned']=={
            'day':day,'words':'tomorrow','read_by':'code','read_on':read_on}


def test_memory_pins_its_time_per_caller_and_decisions_keep_the_plain_time() -> None:
    plain: list[str] = []
    scoped: list[tuple[str, str]] = []

    def operation_time(operation_id: str) -> datetime:
        plain.append(operation_id)
        return FIRST_TIME

    def scoped_operation_time(caller: str, operation_id: str) -> datetime:
        scoped.append((caller, operation_id))
        return FIRST_TIME

    adapter = BrainCoreRecordAdapter(
        operation_time=operation_time,
        scoped_operation_time=scoped_operation_time,
        source_evidence=_evidence,
    )
    fake=LANE_B.FakeGitHub()
    provider = _provider(fake, adapter)
    saved = anyio.run(provider.save, _authorization(Op.MEMORY_SAVE, "write"), _memory_request("scoped-save", "item"))
    head,writes,pins=fake.branch,len(fake.patch_payloads),list(scoped)
    with pytest.raises(BrainRuntimeError) as denied:
        anyio.run(provider.change,_authorization(Op.MEMORY_CHANGE,'write'),
            _memory_change_request('scoped-change',saved.record_id,saved.content_sha256))
    assert denied.value.code=='ACCESS_DENIED'
    assert denied.value.fix=="save it as a new thing with memory.save; Nic's morning review merges it"
    assert fake.branch==head and len(fake.patch_payloads)==writes and scoped==pins
    with bind_write_attribution('hermes-wa'):
        changed=anyio.run(provider.work,_authorization(Op.WORK,'write'),WorkRequest(
            'scoped-change-door','Marked waiting for the fixture','owner','direct',
            ({'move':'wait','thing':saved.record_id,'kind':'needs','who':'the fixture'},)))
    assert changed['status']=='accepted'
    anyio.run(provider.record, _authorization(Op.DECISION_RECORD, "write"), _decision("plain-decision"))

    bridge='bridge-'+hashlib.sha256(b'scoped-save').hexdigest()
    assert scoped==[('nic','work-'+hashlib.sha256(bridge.encode()).hexdigest()),
        ('hermes-wa','work-'+hashlib.sha256(b'scoped-change-door').hexdigest())]
    assert set(plain) == {"plain-decision"}
