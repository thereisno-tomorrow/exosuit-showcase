"""W13 real default replies against frozen W0 caps, using repository fakes only.

The size probe remains W0's 200 independent records, not 200 audited identities.
The moved-over provider scaffold uses W5's separate reconstruction verbatim;
behavior assertions are added after the W12 shared-source gate is released.
"""

from __future__ import annotations

import json
import hashlib
from dataclasses import asdict, dataclass, replace
from datetime import datetime

import pytest
import anyio

from brain_core import work
from brain_core.records import parse_record, render_record
from gremlin_town.api.serialization import to_jsonable
from gremlin_town.mcp.brain_runtime import BrainOperation as Op, MemoryListQuery, MemorySearchQuery, WorkRequest
from tests.mcp.reply_caps import (
    DOOR_REPLY_CAP, FIXTURE_ROOT, REPLY_CAPS, REQUEST_LIST_CAP, memory_reply_texts, request_reply_text,
)
from tests.mcp.test_github_brain_provider import LANE_B, _adapter, _authorization, _provider
from tests.mcp.test_work_door import MESSAGE, call as door_call, setup_door

FIXTURE_NOW = datetime.fromisoformat("2026-09-30T12:20:00+08:00")


@pytest.fixture(scope="module")
def default_reply_texts():
    # Runs the real tools with omitted limits: list 20, search 10, seed 200.
    return memory_reply_texts()


@pytest.mark.parametrize("name", ("bc_memory_list", "bc_memory_search"))
def test_default_reply_fits_frozen_w0_cap(name, default_reply_texts, record_property):
    text = default_reply_texts[name]
    chars = len(text)
    record_property(f"{name}_chars", chars)
    print(f"{name}: {chars} characters; frozen cap {REPLY_CAPS[name]}")
    assert chars <= REPLY_CAPS[name], f"{name}: {chars} > {REPLY_CAPS[name]} characters"


@pytest.fixture
def moved_over_read_fixture(moved_over):
    """Actual W5 records and exact seeded bytes for subsequent W13 read proofs."""
    return _seed_read_records(moved_over)


def _seed_read_records(records):
    """A fake snapshot; callers label any hypothetical changes separately."""
    fake = LANE_B.FakeGitHub()
    raw = {f"records/item/{r.created_at:%Y/%m}/{r.id}.md": render_record(r).encode("utf-8")
           for r in records}
    fake.seed(raw)
    provider = _provider(fake, _adapter())
    provider._clock = lambda: FIXTURE_NOW
    for record in records:
        provider._records.remember_record(_authorization(Op.MEMORY_READ, 'read'), record.id,
                                         f"records/item/{record.created_at:%Y/%m}/{record.id}.md")
    return provider, records, raw


def test_read_fixture_keeps_raw_versions_and_w0_recall_provenance(moved_over_read_fixture):
    _, records, raw = moved_over_read_fixture
    assert len(raw) == len(records)
    for record in records:
        path = f"records/item/{record.created_at:%Y/%m}/{record.id}.md"
        parsed = parse_record(raw[path].decode("utf-8"), path)
        assert parsed.as_dict() == record.as_dict()
    source = json.loads((FIXTURE_ROOT / "messages_30_sep.json").read_text(encoding="utf-8"))
    recalls = {m["id"]: m for m in source["messages"]
               if m["id"] in {"R01", "R03", "R10", "R11", "R12"}}
    assert set(recalls) == {"R01", "R03", "R10", "R11", "R12"}
    assert all(m["category"] == "recall_prompt" and "Invented exhibition scenario" in m["provenance"]
               for m in recalls.values())


@pytest.mark.parametrize("recall,query,title", (
    ("R01", "exhibition permit", "Exhibition permit"),
    ("R03", "venue rental", "Venue rental"),
    ("R10", "Atlas", "Atlas"),
    ("R11", "admin", "Venue rental"),
    ("R12", "venue invoice", "venue invoice"),
))
def test_recall_answers_from_current_shape_with_raw_hashes(recall, query, title, moved_over_read_fixture):
    """Recall prompts ground a caller's literal query; no provider NLP or invented titles."""
    provider, records, raw = moved_over_read_fixture
    index = work.index_things(records)
    result = anyio.run(provider.search, _authorization(Op.MEMORY_SEARCH, "read"), MemorySearchQuery(query, limit=20))
    matching = [reply for reply in result.matches if title.casefold() in reply.title.casefold()]
    assert matching, f"{recall}: no {title} answer"
    for reply in matching:
        thing = index[reply.record_id]
        actual = next(r for r in records if r.id == thing.id)
        path = f"records/item/{actual.created_at:%Y/%m}/{actual.id}.md"
        assert reply.now['is'] == work.kind_of(thing, index)
        assert reply.now['status'] == work.status_of(thing, index, FIXTURE_NOW)
        assert reply.now['dates'] == work.dates_of(thing, index)
        assert reply.content_sha256 == hashlib.sha256(raw[path]).hexdigest()
        assert reply.body == actual.body
        assert reply.body_is == 'first note, old'
        if recall == 'R01':
            assert reply.now['progress']['text'] == '1 of 3'
            assert reply.now['next']['text'] == 'Get a safety checklist from the inspector'
            assert reply.now['next'].get('until', reply.now.get('until')) == '2026-10-08'
            assert 'finish_line' not in reply.now  # R13: Nic never supplied one.
        if recall == 'R12':
            assert reply.now['status'] == 'ended'


def test_admin_finds_every_inherited_label_even_on_a_marked_part(moved_over_read_fixture):
    provider, records, _ = moved_over_read_fixture
    index = work.index_things(records)
    matches = anyio.run(provider.search, _authorization(Op.MEMORY_SEARCH, "read"), MemorySearchQuery('admin', 20)).matches
    found = {reply.record_id for reply in matches}
    labelled = {key for key, record in index.items() if 'exhibition admin' in record.data.get('labels', ())}
    assert labelled <= found
    marked = [record for record in index.values() if record.data.get('review_marks') and record.data.get('parent')]
    for record in marked:
        if 'exhibition admin' in record.data.get('labels', ()):
            assert work.first_id(record) in found


def test_atlas_returns_task_container_and_parts_without_inventing_a_project(moved_over_read_fixture):
    provider, records, _ = moved_over_read_fixture
    index = work.index_things(records)
    replies = anyio.run(provider.search, _authorization(Op.MEMORY_SEARCH, "read"), MemorySearchQuery('Atlas', 20)).matches
    found = {reply.record_id: reply for reply in replies}
    send = next(record for record in index.values() if record.title.startswith('Send Morgan'))
    assert found[work.first_id(send)].now['is'] == 'task'  # First review has not confirmed a project.
    children = {key for key, record in index.items() if record.data.get('parent') == work.first_id(send)}
    assert children and children <= found.keys()
    assert all(found[key].now['under'] == send.title for key in children)


def test_current_read_and_literal_old_read_preserve_distinct_raw_versions(moved_over_read_fixture):
    provider, records, raw = moved_over_read_fixture
    index = work.index_things(records)
    current = next(record for record in index.values() if record.title.startswith('Venue rental'))
    read = anyio.run(provider.read, _authorization(Op.MEMORY_READ, 'read'), current.id)
    assert read.record_id == work.first_id(current)
    assert read.freshness == 'current' and read.canonical_record['id'] == current.id
    actual = next(record for record in records if record.id == current.id)
    assert read.canonical_record == actual.as_dict()  # Never serialize mapped/inherited data as raw.
    old = anyio.run(provider.read, _authorization(Op.MEMORY_READ, 'read'), current.supersedes[0])
    assert old.freshness == 'stale' and old.record_id == current.supersedes[0]
    assert current.id in old.superseded_by
    old_path = f"records/item/{old.updated_at:%Y/%m}/{old.record_id}.md"
    assert old.content_sha256 == hashlib.sha256(raw[old_path]).hexdigest()
    assert old.canonical_record['id'] == old.record_id


def test_first_source_words_are_searchable_after_current_source_changes(moved_over_read_fixture):
    provider, records, _ = moved_over_read_fixture
    index = work.index_things(records)
    result = anyio.run(provider.search, _authorization(Op.MEMORY_SEARCH, 'read'), MemorySearchQuery('Hall', 20))
    venue_rental = next(record for record in index.values() if record.title.startswith('Venue rental'))
    assert work.first_id(venue_rental) in {reply.record_id for reply in result.matches}


def test_hypothetical_marked_part_keeps_labels_but_not_parent_deadline_or_stake(moved_over):
    """Explicit fake overlay of W5 identities, not additional audited owner facts."""
    index = work.index_things(moved_over)
    parent = next(record for record in index.values() if record.title == 'Exhibition permit')
    memo = next(record for record in index.values() if record.title.startswith('Get a safety checklist'))
    deadline = {key: value for key, value in memo.data['dates']['not_before'].items() if key != 'event'}
    changed = tuple(
        replace(record, data={**record.data, 'dates': {'deadline': deadline}}) if record.id == parent.id
        else replace(record, data={**record.data, 'review_marks': [{'mark': 'question', 'text': 'Confirm this part'}]})
        if record.id == memo.id else record
        for record in moved_over
    )
    provider, _, _ = _seed_read_records(changed)
    read = anyio.run(provider.read, _authorization(Op.MEMORY_READ, 'read'), memo.id)
    assert read.now['labels'] == ['exhibition admin']
    assert 'deadline' not in read.now['dates'] and 'stake' not in read.now
    replies = anyio.run(provider.search, _authorization(Op.MEMORY_SEARCH, 'read'), MemorySearchQuery('admin', 20)).matches
    assert work.first_id(memo) in {reply.record_id for reply in replies}
    # Its unmarked sibling does inherit both facts through the sole reader.
    sibling = index['brn_20260930_035056_d00000000000002a']
    read_sibling = anyio.run(provider.read, _authorization(Op.MEMORY_READ, 'read'), sibling.id)
    assert read_sibling.now['dates']['deadline'] == deadline
    assert read_sibling.now['stake'] == parent.data['stake']


def test_hypothetical_explicit_project_choice_returns_atlas_project_and_tasks(moved_over):
    """Project classification is a labelled fake choice; the untouched fixture stays unclassified."""
    index = work.index_things(moved_over)
    container = next(record for record in index.values() if record.title.startswith('Send Morgan'))
    changed = tuple(replace(record, data={**record.data, 'called': 'project'})
                    if record.id == container.id else record for record in moved_over)
    provider, _, _ = _seed_read_records(changed)
    replies = anyio.run(provider.search, _authorization(Op.MEMORY_SEARCH, 'read'), MemorySearchQuery('Atlas', 20)).matches
    found = {reply.record_id: reply for reply in replies}
    assert found[work.first_id(container)].now['is'] == 'project'
    parts = found[work.first_id(container)].now['parts']
    assert parts and all(part['record_id'] in found for part in parts)
    assert all(found[part['record_id']].now['is'] == 'task' for part in parts)


def test_calendar_date_kind_is_reader_appointment_without_raw_override(moved_over_read_fixture):
    provider, records, _ = moved_over_read_fixture
    current = work.index_things(records)['brn_20260929_134536_d000000000000011']
    raw = next(record for record in records if record.id == current.id)
    read = anyio.run(provider.read, _authorization(Op.MEMORY_READ, 'read'), raw.id)
    assert read.now['dates']['appointment']['at'] == '12:00'
    assert read.now['dates']['appointment']['read_on'] == '2026-09-29'
    assert read.canonical_record == raw.as_dict()


def test_formatter_projection_leads_with_now_and_keeps_exact_old_body_last(moved_over_read_fixture):
    provider, records, _ = moved_over_read_fixture
    current = work.index_things(records)['brn_20260929_105119_d000000000000008']
    read = anyio.run(provider.read, _authorization(Op.MEMORY_READ, 'read'), current.id)
    projection = to_jsonable(read)
    assert next(iter(projection)) == 'now'
    assert list(projection)[-2:] == ['body_is', 'body']
    assert projection['body'] == read.body and projection['canonical_record'] == to_jsonable(read.canonical_record)
    assert projection['content_sha256'] == read.content_sha256
    listed = anyio.run(provider.list, _authorization(Op.MEMORY_LIST, 'read'), MemoryListQuery()).items
    assert all(next(iter(to_jsonable(item))) == 'now' for item in listed)
    assert all('answers' not in to_jsonable(item) for item in listed)


def test_other_dataclasses_keep_generic_fields_nulls_empty_values_and_order():
    @dataclass
    class OtherReply:
        absent: None = None
        empty: tuple = ()
        detail: str = 'ordinary reply'

        def reader_reply_fields(self):
            raise AssertionError('An unrelated dataclass must not activate W13 projection')

    reply = OtherReply()
    assert json.dumps(to_jsonable(reply)) == '{"absent": null, "empty": [], "detail": "ordinary reply"}'
    from gremlin_town.mcp.brain_runtime import MemoryRecord, WriteReceipt, WriteReceiptWithCalendar
    ordinary = MemoryRecord('brn_fixture', 'fact', 'Fact', 'Actual content', 'active', 'vault://brain-home',
                            'pin', 'rawhash', FIXTURE_NOW, 'current')
    preference = replace(ordinary, kind='preference')
    receipt = WriteReceipt('fixture-op', 'accepted', 'vault://brain-home', 'brn_fixture', 'pin',
                           'rawhash', None, False)
    calendar = WriteReceiptWithCalendar(**asdict(receipt), calendar={'ack': None, 'questions': []})
    for value in (ordinary, preference, receipt, calendar):
        # Independent frozen generic expansion: every dataclass field remains present.
        generic = {key: to_jsonable(item) for key, item in asdict(value).items()}
        assert json.dumps(to_jsonable(value)) == json.dumps(generic)


def test_projected_nested_null_wait_override_survives_serializer(moved_over_read_fixture):
    provider, records, _ = moved_over_read_fixture
    current = work.index_things(records)['brn_20260929_105119_d000000000000008']
    read = anyio.run(provider.read, _authorization(Op.MEMORY_READ, 'read'), current.id)
    # Explicit semantic null clears a containing wait; it must not inherit that wait.
    answer = {**read.now, 'until': '2026-10-08',
              'next': {'text': 'Explicitly no wait', 'until': None}}
    projected = to_jsonable(replace(read, now=answer))
    assert projected['now']['next']['until'] is None
    assert projected['now']['until'] == '2026-10-08'
    assert json.loads(json.dumps(projected))['now']['next']['until'] is None


@pytest.mark.parametrize('structured', (False, True))
def test_list_exact_title_duplicate_is_visible_once_and_raw_object_unchanged(structured, moved_over_read_fixture):
    provider, _, _ = moved_over_read_fixture
    item = anyio.run(provider.list, _authorization(Op.MEMORY_LIST, 'read'), MemoryListQuery()).items[0]
    selected = {'text': item.title, 'until': None} if structured else item.title
    item = replace(item, now={**item.now, 'next': selected})
    before = asdict(item)
    wire = json.loads(json.dumps(to_jsonable(item)))
    assert 'title' not in wire
    assert wire['now']['next'] == selected
    assert item.title == before['title'] and asdict(item) == before
    assert wire['record_id'] == item.record_id
    assert wire['expected_sha256'] == item.expected_sha256
    if structured:
        assert wire['now']['next']['until'] is None


@pytest.mark.parametrize('difference', ('case', 'punctuation', 'substring', 'part', 'missing', 'missing_text', 'null'))
def test_list_keeps_title_for_nonexact_or_missing_selected_text(difference, moved_over_read_fixture):
    provider, _, _ = moved_over_read_fixture
    item = anyio.run(provider.list, _authorization(Op.MEMORY_LIST, 'read'), MemoryListQuery()).items[0]
    title = 'Exact parent title!'
    choices = {
        'case': 'exact parent title!',
        'punctuation': 'Exact parent title',
        'substring': 'Do Exact parent title! next',
        'part': {'text': 'Different selected part', 'item_ref': 'part-id', 'until': None},
        'missing_text': {'item_ref': 'part-id', 'until': None},
        'null': None,
    }
    answer = {**item.now}
    if difference == 'missing':
        answer.pop('next')
    else:
        answer['next'] = choices[difference]
    item = replace(item, title=title, now=answer)
    wire = json.loads(json.dumps(to_jsonable(item)))
    assert wire['title'] == title
    assert wire['now'] == answer


@pytest.mark.parametrize('structured', (False, True))
def test_read_search_titles_stay_explicit_even_when_next_text_is_exact(structured, moved_over_read_fixture):
    provider, records, _ = moved_over_read_fixture
    current = work.index_things(records)['brn_20260929_105119_d000000000000008']
    read = anyio.run(provider.read, _authorization(Op.MEMORY_READ, 'read'), current.id)
    search = next(reply for reply in anyio.run(provider.search, _authorization(Op.MEMORY_SEARCH, 'read'),
                                             MemorySearchQuery('Venue rental')).matches
                  if reply.record_id == work.first_id(current))
    for reply in (read, search):
        selected = {'text': reply.title, 'until': None} if structured else reply.title
        value = replace(reply, now={**reply.now, 'next': selected})
        before = asdict(value)
        wire = json.loads(json.dumps(to_jsonable(value)))
        assert wire['title'] == reply.title and wire['now']['next'] == selected
        assert wire['content_sha256'] == reply.content_sha256 and wire['version'] == reply.version
        assert wire['body'] == reply.body and wire['body_is'] == 'first note, old'
        assert list(wire)[-2:] == ['body_is', 'body']
        assert asdict(value) == before
        if value.canonical_record is not None:
            assert wire['canonical_record'] == to_jsonable(value.canonical_record)


def test_exhibition_door_reply_fits_frozen_cap(tmp_path, record_property):
    """The real exhibition change through the door factory, measured as its accepted reply."""
    provider, _fake, _pins, log = setup_door(tmp_path)
    try:
        accepted = door_call(provider, WorkRequest("exhibition", MESSAGE, "owner", "direct", (
            {"move": "new", "title": "Exhibition permit"},
            {"move": "start", "thing": "#1"},
            {"move": "parts", "thing": "#1", "parts": [
                {"title": "Text my coordinator", "done": True},
                {"title": "Get a safety checklist from the inspector", "then": True,
                 "when": "8 October", "event": "venue inspection"},
                {"title": "Submit my permit along with the memo on Exhibit Portal", "then": True},
            ]},
        )))
    finally:
        log.close()
    assert accepted["status"] == "accepted" and "1 of 3" in accepted["say"]
    chars = len(json.dumps(accepted))
    record_property("bc_work_chars", chars)
    print(f"bc_work: {chars} characters; frozen cap {DOOR_REPLY_CAP}")
    assert chars <= DOOR_REPLY_CAP, f"bc_work: {chars} > {DOOR_REPLY_CAP} characters"


def test_diablo_default_request_list_fits_frozen_cap(tmp_path, record_property):
    """Diablo's requests.list with no arguments: the handler's default ten rows."""
    text = request_reply_text(tmp_path)
    result = json.loads(text)["result"]
    assert len(result["items"]) == 10 and result["next_cursor"] is not None
    chars = len(text)
    record_property("gremlin_town_requests_list_chars", chars)
    print(f"gremlin_town_requests_list: {chars} characters; frozen cap {REQUEST_LIST_CAP}")
    assert chars <= REQUEST_LIST_CAP, (
        f"gremlin_town_requests_list: {chars} > {REQUEST_LIST_CAP} characters"
    )
