"""Replies show a link at the newest version of its target; stored records are never touched.

A memory change gives a record a new ID and links on other records keep the old one. Read,
list and search follow the supersedes chain from the same snapshot. A reply link keeps the
stored {type, target} shape, so an agent can copy it into memory.save/change.
"""

import hashlib
from types import SimpleNamespace

import anyio
import pytest

import test_github_everyday as everyday
from test_github_brain_provider import _adapter, _authorization, _memory_change_request, _memory_request
from brain_core.records import record_from_mapping, render_record
from gremlin_town.mcp.brain_runtime import BrainOperation as Op, BrainRuntimeError, MemoryListQuery, MemorySearchQuery, WorkRequest
from gremlin_town.mcp.brain_tools import MemoryLinkInput
from gremlin_town.mcp.github_brain_provider import GitHubBrainProvider

WRITE = lambda: _authorization(Op.MEMORY_SAVE, 'write')  # noqa: E731
CHANGE = lambda: _authorization(Op.MEMORY_CHANGE, 'write')  # noqa: E731
READ = lambda: _authorization(Op.MEMORY_READ, 'read')  # noqa: E731
LIST = lambda: _authorization(Op.MEMORY_LIST, 'read')  # noqa: E731
SEARCH = lambda: _authorization(Op.MEMORY_SEARCH, 'read')  # noqa: E731
STRANGER = 'brn_20200101_000000_0000000000000000'


def _plan_saved(**provider_options):
    """Literal pre-work-02 graph; this fixture does not invoke a retired project writer."""
    fake, storage, clock = everyday.setup()
    provider = GitHubBrainProvider(storage, _adapter(operation_time=clock), **provider_options)
    timestamp = everyday.FIRST_TIME.isoformat()
    ids = [f'brn_20260921_040000_{suffix}' for suffix in ('1000abcd', '1001abcd', '1002abcd', '1003abcd')]
    nodes = [
        ('Move house', 'Out of the flat by the end of the month', 'open',
         {'level': 'accepted', 'size': 'goal'}, ()),
        ('Buy boxes', 'Twenty medium boxes', 'open',
         {'level': 'accepted', 'size': 'task', 'block': 'errands',
          'first_step': 'Check the hardware shop hours',
          'timing': {'duration_minutes': 45, 'padded_minutes': 68, 'estimated_by': 'chatgpt-web'}},
         ({'type': 'part_of', 'target': ids[0]},)),
        ('Pack the books', 'Shelves in the study', 'blocked',
         {'level': 'accepted', 'size': 'task', 'block': 'admin',
          'timing': {'duration_minutes': 120, 'padded_minutes': 180, 'estimated_by': 'chatgpt-web'}},
         ({'type': 'part_of', 'target': ids[0]}, {'type': 'blocked_by', 'target': ids[1]})),
        ('Book the movers', 'For the Saturday', 'open',
         {'level': 'accepted', 'size': 'task', 'block': 'admin', 'first_step': 'Get three quotes',
          'timing': {'duration_minutes': 20, 'padded_minutes': 30, 'estimated_by': 'chatgpt-web'}},
         ({'type': 'part_of', 'target': ids[0]},)),
    ]
    seeded, receipts = {}, []
    for record_id, (title, body, state, data, links) in zip(ids, nodes):
        record = record_from_mapping({
            'schema': 'brain.record/v2', 'id': record_id, 'kind': 'item', 'title': title,
            'state': state, 'created_at': timestamp, 'updated_at': timestamp, 'sensitivity': 'private',
            'vaults': ['vault://brain-home'], 'owners': ['brain-home'], 'entities': [],
            'evidence': [{'source_type': 'fixture', 'locator': 'fixture:legacy-plan',
                          'content_sha256': hashlib.sha256(body.encode()).hexdigest(),
                          'excerpt': 'yes, save the move plan'}],
            'supersedes': [], 'dedupe_key': f'fixture:legacy-plan:{record_id}',
            'data': data, 'body': body, 'links': list(links),
            'source': {'said_by': 'owner', 'came_by': 'direct', 'words': 'yes, save the move plan',
                       'recorded_by': 'chatgpt-web'},
        }, 'legacy project fixture')
        path = f'records/item/2026/09/{record_id}.md'
        content = render_record(record).encode('utf-8')
        seeded[path] = content
        provider._records.remember_record(READ(), record_id, path)
        receipts.append(SimpleNamespace(record_id=record_id, content_sha256=hashlib.sha256(content).hexdigest()))
    fake.seed(seeded)
    return fake, provider, SimpleNamespace(goal=receipts[0], steps=tuple(receipts[1:]))


def _change(provider, operation_id, receipt, **changes):
    if 'links' in changes:
        # Arbitrary about-link mutation has no door equivalent; its existing node
        # awaits consumer direction and is excluded from this focused amendment.
        return anyio.run(provider.change, CHANGE(),
            _memory_change_request(operation_id, receipt.record_id, receipt.content_sha256, **changes))
    request = _memory_change_request(operation_id, receipt.record_id, receipt.content_sha256, **changes)
    with pytest.raises(BrainRuntimeError) as denied:
        everyday._as('hermes-wa', provider.change, CHANGE(), request)
    assert denied.value.code == 'ACCESS_DENIED' and denied.value.fix == 'use bc_work'
    original = next(item.record for item in provider._storage.snapshot_records(kinds=frozenset({'item'})).records
                    if item.record.id == receipt.record_id)
    text = 'Check the current plan'
    change = WorkRequest(operation_id, f'{original.title}: {text}', 'owner', 'direct',
                         ({'move': 'next_action', 'thing': receipt.record_id, 'text': text},))
    reply = everyday._as('hermes-wa', provider.work, _authorization(Op.WORK, 'write'), change)
    assert reply['status'] == 'accepted' and reply['remote_observed'] is True
    [thing] = reply['things']
    stored = next(item for item in provider._storage.snapshot_records(kinds=frozenset({'item'})).records
                  if item.record.id == thing['record_id'])
    assert stored.record.body == original.body
    assert stored.record.data['next_action'] == {'text': text, 'done': False}
    return SimpleNamespace(record_id=thing['record_id'], content_sha256=hashlib.sha256(stored.content).hexdigest())


def _views(provider, record_id, query='boxes'):
    """The links of one record as read, list and search show them."""

    read = anyio.run(provider.read, READ(), record_id)
    listed = anyio.run(provider.list, LIST(), MemoryListQuery(state=('open', 'blocked')))
    searched = anyio.run(provider.search, SEARCH(), MemorySearchQuery(query))
    in_list = next(item for item in listed.items if item.record_id == record_id)
    in_search = next(item for item in searched.matches if item.record_id == record_id)
    return read, tuple(in_list.links), tuple(in_search.links)


def test_an_unchanged_target_is_shown_as_saved():
    _, provider, receipt = _plan_saved()
    read, listed, searched = _views(provider, receipt.steps[0].record_id)
    expected = ({'type': 'part_of', 'target': receipt.goal.record_id},)
    assert tuple(read.links) == listed == searched == expected


def test_after_the_goal_changes_read_list_and_search_show_the_new_id_in_the_stored_shape():
    _, provider, receipt = _plan_saved()
    goal, boxes = receipt.goal, receipt.steps[0]
    changed = _change(provider, 'goal-waits', goal)
    assert changed.record_id != goal.record_id
    read, listed, searched = _views(provider, boxes.record_id)
    expected = ({'type': 'part_of', 'target': changed.record_id},)
    assert tuple(read.links) == listed == searched == expected
    # A reply link can be copied straight into a save or change: no extra keys.
    for link in expected:
        MemoryLinkInput(**link)
    # The stored record is untouched: its canonical links still name the ID it was saved with.
    assert read.canonical_record['links'] == [{'type': 'part_of', 'target': goal.record_id}]


def test_a_two_step_chain_resolves_to_the_newest():
    _, provider, receipt = _plan_saved()
    goal, boxes = receipt.goal, receipt.steps[0]
    first = _change(provider, 'goal-waits', goal)
    second = _change(provider, 'goal-open-again', first, state='open')
    assert len({goal.record_id, first.record_id, second.record_id}) == 3
    read, listed, searched = _views(provider, boxes.record_id)
    expected = ({'type': 'part_of', 'target': second.record_id},)
    assert tuple(read.links) == listed == searched == expected


def test_a_link_already_at_the_newest_is_unchanged_and_unknown_targets_stay_as_they_are():
    fake, provider, receipt = _plan_saved()
    goal, boxes, pack = receipt.goal, receipt.steps[0], receipt.steps[1]
    changed = _change(provider, 'goal-waits', goal)
    # The retired change cannot add arbitrary links or write any record.
    about = ({'type': 'about', 'target': changed.record_id}, {'type': 'about', 'target': STRANGER})
    request = _memory_change_request('boxes-links', boxes.record_id, boxes.content_sha256, links=about)
    before, writes_before = fake.branch, len(fake.patch_payloads)
    with pytest.raises(BrainRuntimeError) as denied:
        everyday._as('hermes-wa', provider.change, CHANGE(), request)
    assert denied.value.code == 'ACCESS_DENIED' and denied.value.fix == 'use bc_work'
    assert fake.branch == before and len(fake.patch_payloads) == writes_before

    # These exact links are an existing legacy fixture, not a new link-writing route.
    original = anyio.run(provider.read, READ(), boxes.record_id).canonical_record
    links = [*original['links'], *about]
    record = record_from_mapping({**original, 'id': 'brn_20260921_040000_1005abcd',
                                  'supersedes': [boxes.record_id], 'links': links},
                                 'legacy boxes about-links fixture')
    path = f'records/item/2026/09/{record.id}.md'
    content = render_record(record).encode('utf-8')
    fake.seed({path: content})
    provider._records.remember_record(READ(), record.id, path)
    fixture = SimpleNamespace(record_id=record.id, content_sha256=hashlib.sha256(content).hexdigest())
    read = anyio.run(provider.read, READ(), pack.record_id)
    assert {'type': 'part_of', 'target': changed.record_id} in read.links
    fresh_boxes = next(
        item for item in anyio.run(provider.list, LIST(), MemoryListQuery()).items if item.title == 'Buy boxes'
    )
    assert list(fresh_boxes.links) == [
        {'type': 'part_of', 'target': changed.record_id},
        {'type': 'about', 'target': changed.record_id},
        {'type': 'about', 'target': STRANGER},
    ]
    successor = _change(provider, 'boxes-links-door-next-action', fixture)
    actual = next(item.record for item in provider._storage.snapshot_records(kinds=frozenset({'item'})).records
                  if item.record.id == successor.record_id)
    # Work-02 consumes the old relation into the canonical first-ID parent field.
    assert actual.data['parent'] == goal.record_id
    assert tuple(actual.links) == about
    assert actual.body == original['body']
    # The unknown target is unchanged in read and in search as well.
    newest_boxes = next(item for item in anyio.run(provider.list, LIST(), MemoryListQuery()).items if item.title == 'Buy boxes')
    assert newest_boxes.record_id == boxes.record_id
    # Literal old-ID reads remain historical; use the actual raw successor for this audit.
    read_boxes = anyio.run(provider.read, READ(), successor.record_id)
    assert {'type': 'about', 'target': STRANGER} in read_boxes.links
    found = anyio.run(provider.search, SEARCH(), MemorySearchQuery('boxes')).matches
    assert {'type': 'about', 'target': STRANGER} in next(m for m in found if m.record_id == newest_boxes.record_id).links


def test_reading_a_linked_record_that_was_itself_replaced_is_stale_and_names_its_successor():
    fake, provider, receipt = _plan_saved()
    goal, boxes = receipt.goal, receipt.steps[0]
    new_goal = _change(provider, 'goal-waits', goal)
    # Legacy history is a typed reader fixture. Actual work writes below are
    # checked separately because their new fields belong to the next reader slice.
    request = _memory_change_request('boxes-waits', boxes.record_id, boxes.content_sha256)
    with pytest.raises(BrainRuntimeError) as denied:
        everyday._as('hermes-wa', provider.change, CHANGE(), request)
    assert denied.value.code == 'ACCESS_DENIED' and denied.value.fix == 'use bc_work'
    original = anyio.run(provider.read, READ(), boxes.record_id).canonical_record
    record = record_from_mapping({**original, 'id': 'brn_20260921_040000_1004abcd',
                                  'supersedes': [boxes.record_id]}, 'legacy boxes successor fixture')
    path = f'records/item/2026/09/{record.id}.md'
    content = render_record(record).encode('utf-8')
    fake.seed({path: content})
    provider._records.remember_record(READ(), record.id, path)
    new_boxes = SimpleNamespace(record_id=record.id, content_sha256=hashlib.sha256(content).hexdigest())
    old = anyio.run(provider.read, READ(), boxes.record_id)
    assert old.freshness == 'stale' and tuple(old.superseded_by) == (new_boxes.record_id,)
    assert tuple(old.links) == ({'type': 'part_of', 'target': new_goal.record_id},)
    current = anyio.run(provider.read, READ(), new_boxes.record_id)
    assert current.freshness == 'current' and tuple(current.superseded_by) == ()
    assert tuple(current.links) == ({'type': 'part_of', 'target': new_goal.record_id},)

    _change(provider, 'boxes-door-next-action', new_boxes)


def test_the_list_with_review_on_resolves_links_too():
    _, provider, receipt = _plan_saved(review_enabled=True)
    changed = _change(provider, 'goal-waits', receipt.goal)
    listed = anyio.run(provider.list, LIST(), MemoryListQuery(state=('open', 'blocked')))
    boxes = next(item for item in listed.items if item.record_id == receipt.steps[0].record_id)
    assert tuple(boxes.links) == ({'type': 'part_of', 'target': changed.record_id},)


def test_a_record_without_links_has_a_reader_answer_and_no_effective_links():
    fake, storage, clock = everyday.setup()
    provider = GitHubBrainProvider(storage, _adapter(operation_time=clock))
    saved = anyio.run(provider.save, WRITE(), _memory_request('plain', 'item'))
    read = anyio.run(provider.read, READ(), saved.record_id)
    assert not read.links and read.now['is'] == 'task'
    result = anyio.run(provider.search, SEARCH(), MemorySearchQuery('record'))
    assert not result.matches[0].links and result.matches[0].now['is'] == 'task'


# --- records seeded straight from brain_core.records, for forks, owners and preferences ---------


def _seed(kind, suffix, day, *, updated=None, supersedes=(), links=(), owner='brain-home', title=None):
    """(path, bytes, id) of a record as another writer left it."""

    created = f'2026-09-{day:02d}T04:00:00+00:00'
    compact = created.replace('-', '').replace(':', '').replace('T', '_').split('+')[0]
    record_id = f'brn_{compact}_{suffix}'
    title = title or f'{kind} {suffix}'
    data = {'level': 'accepted', 'size': 'task'} if kind == 'item' else {
        'scope': 'vault://brain-home', 'stability': 'durable', 'evidence': ['fixture:x'], 'correction_history': [],
    }
    mapping = {
        'schema': 'brain.record/v2', 'id': record_id, 'kind': kind, 'title': title,
        'state': 'open' if kind == 'item' else 'active', 'created_at': created,
        'updated_at': updated or created, 'sensitivity': 'private',
        'vaults': ['vault://brain-home'], 'owners': [owner], 'entities': [],
        'evidence': [{'source_type': 'fixture', 'locator': 'fixture:x',
                      'content_sha256': hashlib.sha256(title.encode()).hexdigest(), 'excerpt': 'owner words'}],
        'supersedes': list(supersedes), 'dedupe_key': f'fixture:{kind}:{suffix}', 'data': data, 'body': title,
        'source': {'said_by': 'owner', 'came_by': 'direct', 'words': 'owner words', 'recorded_by': 'brain-home'},
    }
    if links:
        mapping['links'] = [dict(link) for link in links]
    record = record_from_mapping(mapping, 'links fixture')
    path = f'records/{kind}/{created[:4]}/{created[5:7]}/{record_id}.md'
    return path, render_record(record).encode('utf-8'), record_id


def _provider_with(*seeded):
    fake, storage, _ = everyday.setup()
    fake.seed({path: content for path, content, _ in seeded})
    provider = GitHubBrainProvider(storage, _adapter(operation_time=lambda _: everyday.FIRST_TIME))
    for path, _, record_id in seeded:  # a read needs the record's path; production finds it with a locator
        provider._records.remember_record(READ(), record_id, path)
    return provider


@pytest.mark.parametrize('original_owner', ['brain-home', 'another-owner'])
def test_original_only_source_words_survive_archive_at_the_search_pin(monkeypatch, original_owner):
    from dataclasses import replace
    from brain_core.records import parse_record

    original = _seed('item', 'abc12345', 1, owner=original_owner, title='Original task')
    record = parse_record(original[1].decode())
    original = (original[0], render_record(replace(record, source={
        **record.source, 'words': 'quokkaonlysource',
    }, evidence=tuple(replace(evidence, excerpt='quokkaonlysource')
                      for evidence in record.evidence))).encode(), original[2])
    current = _seed('item', 'abc12346', 2, supersedes=(original[2],), title='Current task')
    provider = _provider_with(original, current)
    storage = provider._storage
    expected_ids = [original[2]] if original_owner == 'brain-home' else []
    if expected_ids:
        before = anyio.run(provider.search, SEARCH(), MemorySearchQuery('quokkaonlysource'))
        assert [match.record_id for match in before.matches] == expected_ids
    else:
        with pytest.raises(BrainRuntimeError) as denied:
            anyio.run(provider.search, SEARCH(), MemorySearchQuery('quokkaonlysource'))
        assert denied.value.code == 'brain.contract.invalid'
        assert denied.value.field == 'supersedes' and denied.value.fix
    archived = storage.archive_superseded_records(
        paths=[original[0]], expected_revision=storage.revision().commit_sha)
    lookup = storage.preflight_record_by_id
    revision = storage.revision
    calls = []

    def pinned_lookup(record_id, **kwargs):
        calls.append((record_id, kwargs['revision'].commit_sha, kwargs['include_archive']))
        # A pinned lookup must never consult a newer branch head.
        with monkeypatch.context() as pin:
            pin.setattr(storage, 'revision', lambda: pytest.fail('unpinned first-version lookup'))
            return lookup(record_id, **kwargs)

    monkeypatch.setattr(storage, 'preflight_record_by_id', pinned_lookup)
    if expected_ids:
        after = anyio.run(provider.search, SEARCH(), MemorySearchQuery('quokkaonlysource'))
        assert [match.record_id for match in after.matches] == expected_ids
        assert after.version == archived.commit_sha == revision().commit_sha
    else:
        with pytest.raises(BrainRuntimeError) as denied:
            anyio.run(provider.search, SEARCH(), MemorySearchQuery('quokkaonlysource'))
        assert denied.value.code == 'brain.contract.invalid'
        assert denied.value.field == 'supersedes' and denied.value.fix
    assert calls == [(original[2], archived.commit_sha, True)]
    if expected_ids:
        assert after.matches[0].content_sha256 == hashlib.sha256(current[1]).hexdigest()
        assert after.matches[0].body == 'Current task'
        assert after.matches[0].body_is == 'first note, old'
        assert before.matches[0].content_sha256 == after.matches[0].content_sha256
        monkeypatch.setattr(storage, 'preflight_record_by_id', lookup)
        read = anyio.run(provider.read, READ(), current[2])
        assert read.record_id == original[2]
        assert read.canonical_record['id'] == current[2]


@pytest.mark.parametrize('history', ['missing', 'archived-preference', 'live-preference'])
@pytest.mark.parametrize('query', ['newsourceonly', 'unavailableoriginal'])
def test_search_refuses_unavailable_or_cross_kind_first_history(history, query):
    from dataclasses import replace
    from brain_core.records import parse_record

    original = _seed('preference', 'def12345', 1, title='Other kind')
    current = _seed('item', 'def12346', 2, supersedes=(original[2],), title='Current task')
    record = parse_record(current[1].decode())
    current = (current[0], render_record(replace(record,
        source={**record.source, 'words': 'newsourceonly'},
        evidence=tuple(replace(evidence, excerpt='newsourceonly')
                       for evidence in record.evidence))).encode(), current[2])
    seeds = [current]
    if history != 'missing':
        path = 'archive/' + original[0] if history == 'archived-preference' else original[0]
        seeds.append((path, original[1], original[2]))
    provider = _provider_with(*seeds)
    pin = provider._storage.revision().commit_sha
    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(provider.search, SEARCH(), MemorySearchQuery(query))
    assert refused.value.code == 'brain.contract.invalid'
    assert refused.value.field == 'supersedes'
    assert refused.value.fix == 'restore the exact authorized first item version before searching'
    assert provider._storage.revision().commit_sha == pin


def _shown(provider, record_id, query):
    listed = anyio.run(provider.list, LIST(), MemoryListQuery())
    found = anyio.run(provider.search, SEARCH(), MemorySearchQuery(query))
    read = anyio.run(provider.read, READ(), record_id)
    return (
        tuple(read.links),
        tuple(next(i for i in listed.items if i.record_id == record_id).links),
        tuple(next(m for m in found.matches if m.record_id == record_id).links),
    )


def test_a_fork_resolves_to_the_newest_updated_at_then_the_larger_id():
    old = _seed('item', 'a1a1a1a1', 1, title='Old target')
    branch = _seed('item', 'b2b2b2b2', 2, updated='2026-09-05T04:00:00+00:00', supersedes=(old[2],), title='Branch one')
    newer = _seed('item', 'c3c3c3c3', 3, updated='2026-09-06T04:00:00+00:00', supersedes=(old[2],), title='Branch two')
    holder = _seed('item', 'd4d4d4d4', 4, links=({'type': 'part_of', 'target': old[2]},), title='Holder zebra')
    provider = _provider_with(old, branch, newer, holder)
    expected = ({'type': 'part_of', 'target': newer[2]},)
    assert _shown(provider, holder[2], 'zebra') == (expected,) * 3

    # Same updated_at: the larger ID wins, whichever order the records are stored in.
    tie_a = _seed('item', 'e5e5e5e5', 7, updated='2026-09-10T04:00:00+00:00', supersedes=(old[2],), title='Tie one')
    tie_b = _seed('item', 'f6f6f6f6', 8, updated='2026-09-10T04:00:00+00:00', supersedes=(old[2],), title='Tie two')
    provider = _provider_with(old, tie_a, tie_b, holder)
    assert tie_b[2] > tie_a[2]
    assert _shown(provider, holder[2], 'zebra') == (({'type': 'part_of', 'target': tie_b[2]},),) * 3


def test_another_owners_record_never_resolves_a_link():
    old = _seed('item', 'a1a1a1a1', 1, title='Old target')
    foreign = _seed('item', 'b2b2b2b2', 2, supersedes=(old[2],), owner='other-owner', title='Foreign successor')
    holder = _seed('item', 'd4d4d4d4', 4, links=({'type': 'part_of', 'target': old[2]},), title='Holder zebra')
    provider = _provider_with(old, foreign, holder)
    assert _shown(provider, holder[2], 'zebra') == (({'type': 'part_of', 'target': old[2]},),) * 3


def test_the_preference_list_resolves_links():
    old = _seed('preference', 'a1a1a1a1', 1, title='Old preference')
    new = _seed('preference', 'b2b2b2b2', 2, supersedes=(old[2],), title='New preference')
    holder = _seed('preference', 'c3c3c3c3', 3, links=({'type': 'about', 'target': old[2]},), title='Holder preference')
    provider = _provider_with(old, new, holder)
    listed = anyio.run(provider.list, LIST(), MemoryListQuery(kind='preference'))
    shown = next(item for item in listed.items if item.record_id == holder[2])
    assert tuple(shown.links) == ({'type': 'about', 'target': new[2]},)
