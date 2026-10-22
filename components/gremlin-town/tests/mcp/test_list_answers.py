"""`bc_memory_list` shows The organizer's answers with the task, only while `[review]` is on.

Step E spec §3.1 (Proposed - speed-04; ROA-20): an answer to one of the review's clarifying
questions is its own fact - said_by owner, linked `about` the task - saved through the normal
`bc_memory_save`, so no new field, tool or schema is involved. This drives a real
`GitHubBrainProvider` (fake GitHub transport, real Brain Core) through save, change and list.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import anyio
import pytest

from test_github_brain_provider import (
    FIRST_TIME, LANE_B, _adapter, _authorization, _memory_change_request, _memory_request,
)
from gremlin_town.api.serialization import to_jsonable
from gremlin_town.mcp.brain_runtime import BrainOperation as Op, BrainRuntimeError, MemoryListQuery, WorkRequest
from gremlin_town.mcp.host_identity import bind_write_attribution
from gremlin_town.mcp.github_brain_provider import GitHubBrainProvider

ANSWER_WORDS = "DBS, about the card fee they charged twice"


def _setup():
    fake = LANE_B.FakeGitHub()
    storage = _hub_storage(fake)
    moments: dict[str, object] = {}

    def clock(key):
        return moments.setdefault(key, FIRST_TIME + timedelta(seconds=len(moments)))

    return fake,storage,clock


def _listed(provider: GitHubBrainProvider) -> dict[str, dict]:
    result = anyio.run(provider.list, _authorization(Op.MEMORY_LIST, "read"), MemoryListQuery())
    return {item.record_id: to_jsonable(item) for item in result.items}


def test_an_answer_is_an_ordinary_fact_shown_with_its_task_only_while_review_is_on():
    _,storage,clock = _setup()
    writer = GitHubBrainProvider(storage, _adapter(operation_time=clock))
    save = _authorization(Op.MEMORY_SAVE, "write")
    task = anyio.run(writer.save, save, replace(
        _memory_request("task-bank", "item"),
        title="Call the bank", body="I need to call the bank", words="I need to call the bank",
    ))
    other = anyio.run(writer.save, save, replace(
        _memory_request("task-fix", "item"), title="Fix it", body="Fix it", words="I must fix it",
    ))
    # Saved exactly as Diablo's SKILL.md says: kind fact, said_by owner, words and body his answer.
    reply = anyio.run(writer.save, save, replace(
        _memory_request("answer-bank", "fact"),
        title="Call the bank", body=ANSWER_WORDS, words=ANSWER_WORDS,
        links=({"type": "about", "target": task.record_id},),
    ))
    assert reply.status == "accepted" and reply.remote_observed is True

    off = _listed(GitHubBrainProvider(storage, _adapter(operation_time=clock)))
    assert set(off) == {task.record_id, other.record_id}
    assert all("answers" not in item for item in off.values())

    on = _listed(GitHubBrainProvider(storage, _adapter(operation_time=clock), review_enabled=True))
    assert set(on) == {task.record_id, other.record_id}  # the fact itself is never a list item
    assert on[task.record_id]["answers"] == [{"record_id": reply.record_id, "words": ANSWER_WORDS}]
    assert "answers" not in on[other.record_id]
    # Everything else about the item is exactly what the list returned before.
    assert {key: value for key, value in on[task.record_id].items() if key != "answers"} == off[task.record_id]


def test_the_answer_stays_with_the_task_after_the_task_changes():
    fake,storage,clock = _setup()
    provider = GitHubBrainProvider(storage, _adapter(operation_time=clock), review_enabled=True)
    save = _authorization(Op.MEMORY_SAVE, "write")
    task = anyio.run(provider.save, save, replace(
        _memory_request("task-bank", "item"),
        title="Call the bank", body="I need to call the bank", words="I need to call the bank",
    ))
    reply = anyio.run(provider.save, save, replace(
        _memory_request("answer-bank", "fact"),
        title="Call the bank", body=ANSWER_WORDS, words=ANSWER_WORDS,
        links=({"type": "about", "target": task.record_id},),
    ))
    listed = _listed(provider)
    head,writes=fake.branch,len(fake.patch_payloads)
    with pytest.raises(BrainRuntimeError) as denied:
        anyio.run(provider.change,_authorization(Op.MEMORY_CHANGE,'write'),_memory_change_request(
            'wait-bank',task.record_id,listed[task.record_id]['expected_sha256'],words='Waiting for the bank to open'))
    assert denied.value.code=='ACCESS_DENIED'
    assert denied.value.fix=="save it as a new thing with memory.save; Nic's morning review merges it"
    assert fake.branch==head and len(fake.patch_payloads)==writes
    with bind_write_attribution('hermes-wa'):
        changed=anyio.run(provider.work,_authorization(Op.WORK,'write'),WorkRequest(
            'wait-bank-door','Waiting for the bank to open','owner','direct',
            ({'move':'wait','thing':task.record_id,'kind':'not_before','event':'the bank to open',
                'reading':{'event':'the bank to open'}},)))
    changed_id=changed['things'][0]['record_id']
    assert changed['status']=='accepted' and changed_id!=task.record_id

    after = _listed(provider)
    assert set(after) == {task.record_id}  # W13 outward identity is the lasting first ID.
    # Native work stores the wait as a fact, with the work reader explaining it.
    from brain_core import work
    records=[entry.record for entry in storage.snapshot_records(kinds=frozenset({'item'})).records]
    current=next(record for record in records if record.id==changed_id)
    assert after[task.record_id]['state']==current.state=='open'
    assert current.data['dates']['not_before']['event']=='the bank to open'
    assert work.explain(current.id,work.index_things(records),FIRST_TIME)['status']=='waiting'
    assert after[task.record_id]["answers"] == [{"record_id": reply.record_id, "words": ANSWER_WORDS}]
    assert after[task.record_id]['now']['status'] == 'waiting'


def _hub_storage(fake):
    """A storage as the hub builds it, with an object cache: Brain Core refuses the hub's 5,000-file scan
    cap without one (docs/product/2026-09-30-brain-home-storage-limit-plan.md, layer 1)."""

    from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache

    return GitHubBrainHomeStorage(
        repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN, transport=fake,
        object_cache=GitHubObjectCache(),
    )
