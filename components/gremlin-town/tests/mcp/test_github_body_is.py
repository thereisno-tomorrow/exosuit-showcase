"""Read and search replies say a record's body is its original note; the fields win.

A body is written once at the first save. Agents later change first_step, state and dates but
cannot rewrite it, so it goes stale (the venue rental item, 30 Sep 2026). Read and search replies
carry a plain ``body_is`` beside ``body`` when the body has fields next to it; list replies carry
no body and are left alone. ``body`` and ``canonical_record`` are exactly as before.
"""

from types import SimpleNamespace
import hashlib

import anyio
import pytest

from brain_core.records import parse_record, record_from_mapping, render_record

import test_github_everyday as everyday
from test_github_brain_provider import _adapter, _authorization, _memory_change_request, _memory_request
from gremlin_town.api.serialization import to_jsonable
from gremlin_town.mcp.brain_runtime import (
    BODY_IS_NOTE, BrainOperation as Op, MemoryListQuery, MemoryRecord, MemoryRecordWithBodyNote,
    MemorySearchQuery, BrainRuntimeError, WorkRequest,
)
from gremlin_town.mcp.github_brain_provider import GitHubBrainProvider, _memory_reply

WRITE = lambda: _authorization(Op.MEMORY_SAVE, 'write')  # noqa: E731
CHANGE = lambda: _authorization(Op.MEMORY_CHANGE, 'write')  # noqa: E731
READ = lambda: _authorization(Op.MEMORY_READ, 'read')  # noqa: E731
LIST = lambda: _authorization(Op.MEMORY_LIST, 'read')  # noqa: E731
SEARCH = lambda: _authorization(Op.MEMORY_SEARCH, 'read')  # noqa: E731
NOTE = 'original note from the first save; first_step, state and dates are current'


def _provider():
    fake, storage, clock = everyday.setup()
    return GitHubBrainProvider(storage, _adapter(operation_time=clock))


@pytest.mark.parametrize('body', ['  leading note\n\ninternal paragraph  \n', 'original note\n\n', 'note\r\n', ''])
def test_bridge_stores_exact_render_parse_body_and_replays_original_input(body):
    fake, storage, clock = everyday.setup()
    # Reuse the complete fake commit metadata needed by exact history verification.
    from test_github_brain_provider import _hub_storage
    provider = GitHubBrainProvider(_hub_storage(fake), _adapter(operation_time=clock))
    request = _memory_request('body-roundtrip', 'item', body=body, words='save this note')
    first = everyday._as('chatgpt-web', provider.save, WRITE(), request)
    match = storage.preflight_record_by_id(first.record_id).match
    assert match is not None
    parsed = parse_record(match.content.decode(), 'stored original body')
    assert parsed.body == body.rstrip('\n')
    assert render_record(parsed).encode() == match.content
    head = fake.branch
    replay = everyday._as('chatgpt-web', provider.save, WRITE(), request)
    assert replay.status == 'duplicate' and replay.content_sha256 == first.content_sha256
    assert replay.record_id == first.record_id and replay.version == first.version and fake.branch == head
    from dataclasses import replace
    with pytest.raises(BrainRuntimeError) as conflict:
        everyday._as('chatgpt-web', provider.save, WRITE(), replace(request, body=body + '\n'))
    assert conflict.value.code == 'brain.conflict' and conflict.value.fix
    assert fake.branch == head


def _legacy_item(*, record_id, first_step=None, supersedes=()):
    """Exact pre-work-02 item fields keep the existing reader contract under test."""
    body = 'Waiting on the exhibition admin chat'
    data = {'level': 'accepted', 'size': 'task'}
    if first_step is not None:
        data['first_step'] = first_step
    return record_from_mapping({
        'schema': 'brain.record/v2', 'id': record_id, 'kind': 'item', 'title': 'Venue rental',
        'state': 'open', 'created_at': everyday.FIRST_TIME.isoformat(),
        'updated_at': everyday.FIRST_TIME.isoformat(), 'sensitivity': 'private',
        'vaults': ['vault://brain-home'], 'owners': ['brain-home'], 'entities': [],
        'evidence': [{'source_type': 'fixture', 'locator': 'fixture:venue rental',
                      'content_sha256': hashlib.sha256(body.encode()).hexdigest(),
                      'excerpt': body}],
        'supersedes': list(supersedes), 'dedupe_key': f'fixture:venue_rental:{record_id}',
        'data': data, 'body': body,
        'source': {'said_by': 'owner', 'came_by': 'direct', 'words': body,
                   'recorded_by': 'chatgpt-web'},
    }, 'legacy venue rental fixture')


def _saved_item():
    fake, storage, clock = everyday.setup()
    provider = GitHubBrainProvider(storage, _adapter(operation_time=clock))
    record = _legacy_item(record_id='brn_20260921_040000_2000abcd')
    path = f'records/item/2026/09/{record.id}.md'
    content = render_record(record).encode('utf-8')
    fake.seed({path: content})
    provider._records.remember_record(READ(), record.id, path)
    return provider, SimpleNamespace(record_id=record.id,
        content_sha256=hashlib.sha256(content).hexdigest(),
        fixture_fake=fake, fixture_files={path: content})


def _legacy_changed_item(provider, saved, operation_id, *, caller):
    request = _memory_change_request(operation_id, saved.record_id, saved.content_sha256,
                                    first_step='Book an Hall office appointment')
    with pytest.raises(BrainRuntimeError) as denied:
        everyday._as(caller, provider.change, CHANGE(), request)
    assert denied.value.code == 'ACCESS_DENIED'
    assert denied.value.fix == ('use bc_work' if caller == 'hermes-wa' else
                               "save it as a new thing with memory.save; Nic's morning review merges it")
    record = _legacy_item(record_id='brn_20260921_040000_2001abcd',
                          first_step='Book an Hall office appointment', supersedes=(saved.record_id,))
    path = f'records/item/2026/09/{record.id}.md'
    content = render_record(record).encode('utf-8')
    saved.fixture_fake.seed({**saved.fixture_files, path: content})
    provider._records.remember_record(READ(), record.id, path)
    return SimpleNamespace(record_id=record.id, content_sha256=hashlib.sha256(content).hexdigest())


def _door_step_keeps_the_original_body(provider, saved, operation_id):
    text = 'Book an Hall office appointment'
    request = WorkRequest(operation_id, f'Venue rental: {text}', 'owner', 'direct',
                          ({'move': 'next_action', 'thing': saved.record_id, 'text': text},))
    reply = everyday._as('hermes-wa', provider.work, _authorization(Op.WORK, 'write'), request)
    assert reply['status'] == 'accepted' and reply['remote_observed'] is True
    [thing] = reply['things']
    actual = next(item.record for item in provider._storage.snapshot_records(kinds=frozenset({'item'})).records
                  if item.record.id == thing['record_id'])
    assert actual.body == 'Waiting on the exhibition admin chat'
    assert actual.data['next_action'] == {'text': text, 'done': False}


def test_the_note_text_is_the_one_agents_are_told():
    assert BODY_IS_NOTE == NOTE


def test_read_and_search_carry_body_is_beside_an_unchanged_body():
    provider, saved = _saved_item()
    saved = _legacy_changed_item(provider, saved, 'venue-rental-step', caller='hermes-wa')
    read = anyio.run(provider.read, READ(), saved.record_id)
    found = anyio.run(provider.search, SEARCH(), MemorySearchQuery('venue rental')).matches[0]
    for reply in (read, found):
        assert reply.body_is == 'first note, old'
        assert reply.body == 'Waiting on the exhibition admin chat'
        assert to_jsonable(reply)['body_is'] == 'first note, old'
        assert not reply.links
        assert reply.now is not None
    assert read.canonical_record['body'] == 'Waiting on the exhibition admin chat'
    _door_step_keeps_the_original_body(provider, saved, 'venue-rental-door-step')


def test_a_fact_with_a_body_gets_no_note_because_its_body_is_the_content():
    provider = _provider()
    saved = anyio.run(provider.save, WRITE(), _memory_request('fact-one', 'fact', title='Zebra fact', body='Nic prefers window seats'))
    read = anyio.run(provider.read, READ(), saved.record_id)
    found = anyio.run(provider.search, SEARCH(), MemorySearchQuery('zebra')).matches[0]
    for reply in (read, found):
        assert type(reply) is MemoryRecord
        assert reply.body == 'Nic prefers window seats'
        assert 'body_is' not in to_jsonable(reply)


def test_an_item_with_a_body_and_only_a_state_gets_the_reader_answer():
    provider, saved = _saved_item()
    read = anyio.run(provider.read, READ(), saved.record_id)
    assert read.body_is == 'first note, old' and read.now['status'] == 'up'
    assert read.body == read.canonical_record['body']


def test_a_changed_record_still_shows_the_old_body_with_the_note_and_its_new_state():
    provider, saved = _saved_item()
    changed = _legacy_changed_item(provider, saved, 'venue-rental-first-step', caller='chatgpt-web')
    read = anyio.run(provider.read, READ(), changed.record_id)
    assert read.body == 'Waiting on the exhibition admin chat'
    assert read.body_is == 'first note, old'
    assert read.canonical_record['data']['first_step'] == 'Book an Hall office appointment'
    _door_step_keeps_the_original_body(provider, changed, 'venue-rental-door-first-step')


def test_list_replies_carry_no_body_and_no_note():
    provider, _ = _saved_item()
    listed = anyio.run(provider.list, LIST(), MemoryListQuery()).items
    assert listed and not any(hasattr(item, 'body_is') or hasattr(item, 'body') for item in listed)


def test_a_record_with_links_still_shows_them_and_the_note():
    _, provider, receipt = _plan_saved()
    step = receipt.steps[0].record_id
    read = anyio.run(provider.read, READ(), step)
    found = next(m for m in anyio.run(provider.search, SEARCH(), MemorySearchQuery('boxes')).matches if m.record_id == step)
    for reply in (read, found):
        assert tuple(reply.links) == ({'type': 'part_of', 'target': receipt.goal.record_id},)
        assert reply.body_is == 'first note, old'
        assert to_jsonable(reply)['body_is'] == 'first note, old'


def _plan_saved():
    from test_github_links_at_newest import _plan_saved as legacy_plan

    return legacy_plan()


def _stub(*, body, state='', data=None):
    return SimpleNamespace(body=body, state=state, data=data or {})


def _view():
    return dict(
        record_id='brn_x', kind='item', title='T', body='b', state='open', owner_locator='vault://x',
        version='v', content_sha256='h', updated_at=everyday.FIRST_TIME, freshness='current',
    )


def test_no_body_or_no_fields_keeps_the_old_shape():
    for record in (
        _stub(body='', state='open', data={'first_step': 'Do it', 'timing': {'due': '2026-10-01'}}),
        _stub(body='original words', state='open', data={}),
        _stub(body='original words', state='open', data={'level': 'accepted'}),
    ):
        reply = _memory_reply(record, _view(), ())
        assert type(reply) is MemoryRecord
        assert 'body_is' not in to_jsonable(reply)


def test_a_first_step_or_timing_beside_a_body_is_enough():
    for record in (
        _stub(body='b', data={'first_step': 'Do it'}),
        _stub(body='b', data={'timing': {'due': '2026-10-01'}}),
    ):
        assert type(_memory_reply(record, _view(), ())) is MemoryRecordWithBodyNote
