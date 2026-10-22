from dataclasses import replace
from types import SimpleNamespace
from datetime import UTC, timedelta
import hashlib
import anyio
import pytest

from brain_core.records import record_from_mapping, render_record
from brain_core.timewords import SINGAPORE
from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache
from test_github_brain_provider import (
    LANE_B, _adapter, _authorization, _memory_request, _memory_change_request, FIRST_TIME,
)
from gremlin_town.mcp.github_brain_provider import BrainCoreRecordAdapter, GitHubBrainProvider
from gremlin_town.mcp.github_continuity import GitHubContinuityProvider
from gremlin_town.mcp.brain_runtime import (
    BrainOperation as Op, BrainRuntimeError, MemoryListQuery, MemorySearchQuery,
    ContinuityStartRequest, ContinuityAppendRequest, ContinuityCloseRequest, ContinuityRecoverRequest,
    ContinuityListRequest, WorkRequest, BrainCaller, OwnerSelector,
)


def setup():
    fake=LANE_B.FakeGitHub()
    storage=_hub_storage(fake)
    moments={}
    def clock(key):
        return moments.setdefault(key,FIRST_TIME+timedelta(seconds=len(moments)))
    return fake,storage,clock


def test_search_finds_saved_memory_from_a_fresh_provider_without_record_id():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),replace(_memory_request('clanker-proof','item'),title='Clanker idea',body='A token launch idea.',words='A token launch idea.'))
    fresh=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    before=len(fake.patch_payloads)
    result=anyio.run(fresh.search,_authorization(Op.MEMORY_SEARCH,'read'),MemorySearchQuery('clanker'))
    assert [m.record_id for m in result.matches]==[saved.record_id]
    assert result.version==saved.version
    assert not result.truncated
    assert len(fake.patch_payloads)==before


def test_search_does_not_return_another_owner():
    _,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),_memory_request('private-proof','item'))
    wrong=replace(_authorization(Op.MEMORY_SEARCH,'read'),owner_id='other-owner')
    assert not anyio.run(provider.search,wrong,MemorySearchQuery('record')).matches


def test_search_ranks_a_title_match_above_a_newer_body_only_match():
    # Search ranking (docs/product/2026-09-26-gtbc-speed-goal.md Wave 3): a
    # title hit outranks a body-only hit even when the body-only record is
    # more recent - today's code would rank the newer body-only record first.
    _,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    title_hit=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('title-hit','item'),title='Clanker idea',body='An unrelated plan.',words='An unrelated plan.'))
    body_hit=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('body-hit','item'),title='Grocery list',body='Mentions the clanker plan.',words='Mentions the clanker plan.'))
    result=anyio.run(provider.search,_authorization(Op.MEMORY_SEARCH,'read'),MemorySearchQuery('clanker'))
    assert [m.record_id for m in result.matches]==[title_hit.record_id,body_hit.record_id]


def test_search_ties_without_a_title_hit_keep_the_newer_record_first():
    # Ties (no title hit on either side) keep today's order: newest first.
    _,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    older=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('body-older','item'),title='Task one',body='Mentions the clanker plan.',words='Mentions the clanker plan.'))
    newer=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('body-newer','item'),title='Task two',body='Also mentions the clanker plan.',words='Also mentions the clanker plan.'))
    result=anyio.run(provider.search,_authorization(Op.MEMORY_SEARCH,'read'),MemorySearchQuery('clanker'))
    assert [m.record_id for m in result.matches]==[newer.record_id,older.record_id]


def test_search_truncated_flag_is_unaffected_by_title_ranking():
    _,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    for suffix in ('one','two','three'):
        anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),
            replace(_memory_request(f'trunc-{suffix}','item'),title=f'Clanker {suffix}',body='Body text.',words='Body text.'))
    result=anyio.run(provider.search,_authorization(Op.MEMORY_SEARCH,'read'),MemorySearchQuery('clanker',limit=2))
    assert len(result.matches)==2
    assert result.truncated


def _aliased_item(suffix,*,title,body,aliases,timestamp):
    # Proposed - Nic approves before release speed-02 goes live
    # (docs/product/2026-09-26-gtbc-speed-goal.md Wave 3). No save tool sets
    # data.aliases yet, so this fixture is built straight from
    # brain_core.records instead of through MemorySaveRequest.
    compact=timestamp.replace("-","").replace(":","").replace("T","_").split("+")[0]
    data={"level":"accepted","size":"task"}
    if aliases is not None:
        data["aliases"]=aliases
    record=record_from_mapping({
        "schema":"brain.record/v2","id":f"brn_{compact}_{suffix}","kind":"item","title":title,
        "state":"open","created_at":timestamp,"updated_at":timestamp,"sensitivity":"private",
        "vaults":["vault://brain-home"],"owners":["brain-home"],"entities":[],
        "evidence":[{"source_type":"fixture","locator":"fixture:item",
            "content_sha256":hashlib.sha256(body.encode()).hexdigest(),"excerpt":"owner words"}],
        "supersedes":[],"dedupe_key":f"fixture:item:{suffix}","data":data,"body":body,
        "source":{"said_by":"owner","came_by":"direct","words":"owner words","recorded_by":"brain-home"},
    },"aliased item fixture")
    return render_record(record).encode("utf-8")


def test_search_ranks_an_alias_hit_like_a_title_hit():
    fake,storage,_=setup()
    alias_hit=_aliased_item("a1a1a1a1",title="Grocery list",body="Nothing relevant here.",
        aliases=["clanker"],timestamp="2026-09-21T04:00:00+00:00")
    body_hit=_aliased_item("b2b2b2b2",title="Different task",body="Mentions the clanker plan.",
        aliases=None,timestamp="2026-09-22T04:00:00+00:00")
    fake.seed({
        "records/item/2026/09/brn_20260921_040000_a1a1a1a1.md":alias_hit,
        "records/item/2026/09/brn_20260922_040000_b2b2b2b2.md":body_hit,
    })
    provider=GitHubBrainProvider(storage,_adapter(operation_time=lambda _:FIRST_TIME))
    result=anyio.run(provider.search,_authorization(Op.MEMORY_SEARCH,'read'),MemorySearchQuery('clanker'))
    assert [m.record_id for m in result.matches]==[
        "brn_20260921_040000_a1a1a1a1","brn_20260922_040000_b2b2b2b2",
    ]


# --- Wave 3: near-duplicates at save (ROA-11) ---------------------------------


def test_near_duplicate_a_few_hours_apart_is_named_in_the_second_receipt():
    fake,storage,_=setup()
    provider=GitHubBrainProvider(storage,_adapter({'dup-first':FIRST_TIME,'dup-second':FIRST_TIME+timedelta(hours=4)}))
    write=_authorization(Op.MEMORY_SAVE,'write')
    first=anyio.run(provider.save,write,replace(_memory_request('dup-first','item'),title='Buy ink',words='Buy printer ink'))
    second=anyio.run(provider.save,write,replace(_memory_request('dup-second','item'),title='Buy ink again',words='Buy printer ink'))
    assert _maybe_same(provider,first.record_id)==()
    assert _maybe_same(provider,second.record_id)==(first.record_id,)
    assert first.status=='accepted' and second.status=='accepted'
    assert first.record_id!=second.record_id


def test_near_duplicate_matches_despite_case_spacing_and_trailing_punctuation():
    fake,storage,_=setup()
    provider=GitHubBrainProvider(storage,_adapter({'dup-surface-a':FIRST_TIME,'dup-surface-b':FIRST_TIME+timedelta(hours=1)}))
    write=_authorization(Op.MEMORY_SAVE,'write')
    first=anyio.run(provider.save,write,replace(_memory_request('dup-surface-a','item'),words='Buy printer ink'))
    second=anyio.run(provider.save,write,replace(_memory_request('dup-surface-b','item'),words='  buy   PRINTER ink!!  '))
    assert _maybe_same(provider,second.record_id)==(first.record_id,)


def test_near_duplicate_window_excludes_a_save_25_hours_earlier():
    fake,storage,_=setup()
    provider=GitHubBrainProvider(storage,_adapter({'dup-window-a':FIRST_TIME,'dup-window-b':FIRST_TIME+timedelta(hours=25)}))
    write=_authorization(Op.MEMORY_SAVE,'write')
    anyio.run(provider.save,write,replace(_memory_request('dup-window-a','item'),words='Buy printer ink'))
    outside=anyio.run(provider.save,write,replace(_memory_request('dup-window-b','item'),words='Buy printer ink'))
    assert _maybe_same(provider,outside.record_id)


def test_near_duplicate_ignores_another_owners_record():
    fake,storage,_=setup()
    provider=GitHubBrainProvider(storage,_adapter({'dup-owner-a':FIRST_TIME,'dup-owner-b':FIRST_TIME+timedelta(hours=1)}))
    write=_authorization(Op.MEMORY_SAVE,'write')
    other_write=replace(write,owner_id='other-owner')
    anyio.run(provider.save,write,replace(_memory_request('dup-owner-a','item'),words='Buy printer ink'))
    theirs=anyio.run(provider.save,other_write,replace(_memory_request('dup-owner-b','item'),words='Buy printer ink'))
    assert _maybe_same(provider,theirs.record_id)==()


def test_near_duplicate_ignores_a_superseded_record():
    fake,storage,_=setup()
    provider=GitHubBrainProvider(storage,_adapter({
        'dup-sup-a':FIRST_TIME,'dup-sup-change':FIRST_TIME+timedelta(hours=1),'dup-sup-b':FIRST_TIME+timedelta(hours=2),
    }))
    write=_authorization(Op.MEMORY_SAVE,'write')
    original=anyio.run(provider.save,write,replace(_memory_request('dup-sup-a','item'),words='Call the dentist tomorrow'))
    _denied_change(fake,provider,_memory_change_request('dup-sup-change',original.record_id,original.content_sha256))
    words='I finished Record title'
    _door(provider,WorkRequest('dup-sup-change',words,'owner','direct',
        ({'move':'tick','thing':original.record_id,'what':'thing','words':words},)))
    later=anyio.run(provider.save,write,replace(_memory_request('dup-sup-b','item'),words='Call the dentist tomorrow!'))
    assert _maybe_same(provider,later.record_id)==()


def test_near_duplicate_ignores_an_agent_worded_old_record():
    fake,storage,_=setup()
    provider=GitHubBrainProvider(storage,_adapter({'dup-agent-a':FIRST_TIME,'dup-agent-b':FIRST_TIME+timedelta(hours=1)}))
    write=_authorization(Op.MEMORY_SAVE,'write')
    anyio.run(provider.save,write,replace(
        _memory_request('dup-agent-a','item',said_by='agent',came_by='agent_guess',speaker='fixture-agent',level='suggested'),
        words='Buy printer ink',
    ))
    owner_save=anyio.run(provider.save,write,replace(_memory_request('dup-agent-b','item'),words='Buy printer ink'))
    assert _maybe_same(provider,owner_save.record_id)


def test_near_duplicate_is_not_checked_when_the_new_save_is_agent_worded():
    # Work-02's maybe_same mark compares titles regardless of the source role.
    fake,storage,_=setup()
    provider=GitHubBrainProvider(storage,_adapter({'dup-newagent-a':FIRST_TIME,'dup-newagent-b':FIRST_TIME+timedelta(hours=1)}))
    write=_authorization(Op.MEMORY_SAVE,'write')
    anyio.run(provider.save,write,replace(_memory_request('dup-newagent-a','item'),words='Buy printer ink'))
    agent_save=anyio.run(provider.save,write,replace(
        _memory_request('dup-newagent-b','item',said_by='agent',came_by='agent_guess',speaker='fixture-agent',level='suggested'),
        words='Buy printer ink',
    ))
    assert _maybe_same(provider,agent_save.record_id)


def test_brain_home_continuity_survives_fresh_provider_and_retries():
    fake,storage,clock=setup()
    provider=GitHubContinuityProvider(storage,clock)
    start=ContinuityStartRequest('start-proof','conversation-one','Remember the Clanker idea',())
    first=anyio.run(provider.start,_authorization(Op.CONTINUITY_START,'write'),start)
    assert first.receipt.remote_observed
    fresh=GitHubContinuityProvider(storage,lambda _: (_ for _ in ()).throw(AssertionError('Replay must not ask for a new clock')))
    duplicate=anyio.run(fresh.start,_authorization(Op.CONTINUITY_START,'write'),start)
    assert duplicate.receipt.status=='duplicate'
    assert duplicate.record_id==first.record_id
    with pytest.raises(BrainRuntimeError,match='different input'):
        anyio.run(provider.start,_authorization(Op.CONTINUITY_START,'write'),replace(start,intent='Changed'))
    append=ContinuityAppendRequest('append-proof','conversation-one','next_step','Explore creator rewards')
    fake.lost_apply_once=True
    added=anyio.run(provider.append,_authorization(Op.CONTINUITY_APPEND,'write'),append)
    assert added.receipt.remote_observed
    brief=anyio.run(fresh.recover,_authorization(Op.CONTINUITY_RECOVER,'read'),ContinuityRecoverRequest())
    assert brief.intent=='Remember the Clanker idea'
    assert brief.next_step=='Explore creator rewards'
    assert brief.record_id==added.record_id
    duplicate=anyio.run(fresh.append,_authorization(Op.CONTINUITY_APPEND,'write'),append)
    assert duplicate.receipt.status=='duplicate'
    close=ContinuityCloseRequest('close-proof','conversation-one')
    anyio.run(provider.close,_authorization(Op.CONTINUITY_CLOSE,'write'),close)
    with pytest.raises(BrainRuntimeError):
        anyio.run(fresh.recover,_authorization(Op.CONTINUITY_RECOVER,'read'),ContinuityRecoverRequest())


def test_continuity_denies_wrong_owner_and_read_only_write():
    _,storage,clock=setup()
    provider=GitHubContinuityProvider(storage,clock)
    start=ContinuityStartRequest('start-one','one','Authorized intent',())
    with pytest.raises(BrainRuntimeError):
        anyio.run(provider.start,_authorization(Op.CONTINUITY_START,'read'),start)
    anyio.run(provider.start,_authorization(Op.CONTINUITY_START,'write'),start)
    with pytest.raises(BrainRuntimeError):
        anyio.run(provider.recover,replace(_authorization(Op.CONTINUITY_RECOVER,'read'),owner_id='another-owner'),ContinuityRecoverRequest())


def test_concurrent_continuity_append_rebases_without_losing_other_event():
    _,storage,clock=setup()
    other=GitHubContinuityProvider(storage,clock)
    anyio.run(other.start,_authorization(Op.CONTINUITY_START,'write'),ContinuityStartRequest('seed','shared','Shared context',()))
    class RacingStorage:
        raced=False
        def snapshot_records(self, **kwargs):
            return storage.snapshot_records(**kwargs)
        def publish_record(self, **kwargs):
            if not self.raced:
                self.raced=True
                other._write(_authorization(Op.CONTINUITY_APPEND,'write'),ContinuityAppendRequest('writer-two','shared','effect','Second effect'),Op.CONTINUITY_APPEND)
            return storage.publish_record(**kwargs)
    first=GitHubContinuityProvider(RacingStorage(),clock)
    anyio.run(first.append,_authorization(Op.CONTINUITY_APPEND,'write'),ContinuityAppendRequest('writer-one','shared','effect','First effect'))
    recovered=anyio.run(other.recover,_authorization(Op.CONTINUITY_RECOVER,'read'),ContinuityRecoverRequest())
    assert set(recovered.effects)=={'First effect','Second effect'}


def test_semantic_noop_still_binds_operation_input():
    _,storage,clock=setup()
    provider=GitHubContinuityProvider(storage,clock)
    anyio.run(provider.start,_authorization(Op.CONTINUITY_START,'write'),ContinuityStartRequest('seed','shared','Shared context',()))
    request=ContinuityAppendRequest('unchanged','shared','intent','Shared context')
    accepted=anyio.run(provider.append,_authorization(Op.CONTINUITY_APPEND,'write'),request)
    assert accepted.receipt.remote_observed
    assert anyio.run(provider.append,_authorization(Op.CONTINUITY_APPEND,'write'),request).receipt.status=='duplicate'
    with pytest.raises(BrainRuntimeError):
        anyio.run(provider.append,_authorization(Op.CONTINUITY_APPEND,'write'),replace(request,value='Different context'))


def test_recover_by_session_id_returns_that_tasks_note_on_brain_home():
    _, storage, clock = setup()
    provider = GitHubContinuityProvider(storage, clock)
    anyio.run(provider.start, _authorization(Op.CONTINUITY_START, 'write'),
              ContinuityStartRequest('seed-a', 'session-a', 'Ship step A', ()))
    anyio.run(provider.start, _authorization(Op.CONTINUITY_START, 'write'),
              ContinuityStartRequest('seed-b', 'session-b', 'Ship step B', ()))
    latest = anyio.run(provider.recover, _authorization(Op.CONTINUITY_RECOVER, 'read'), ContinuityRecoverRequest())
    assert latest.session_id == 'session-b'
    by_session = anyio.run(provider.recover, _authorization(Op.CONTINUITY_RECOVER, 'read'),
                           ContinuityRecoverRequest(2_500, 'session-a'))
    assert by_session.session_id == 'session-a'
    assert by_session.intent == 'Ship step A'
    with pytest.raises(BrainRuntimeError):
        anyio.run(provider.recover, _authorization(Op.CONTINUITY_RECOVER, 'read'),
                 ContinuityRecoverRequest(2_500, 'missing-session'))


def test_continuity_list_returns_open_notes_newest_first_on_brain_home():
    _, storage, clock = setup()
    provider = GitHubContinuityProvider(storage, clock)
    anyio.run(provider.start, _authorization(Op.CONTINUITY_START, 'write'),
              ContinuityStartRequest('seed-a', 'session-a', 'Ship step A', ()))
    anyio.run(provider.start, _authorization(Op.CONTINUITY_START, 'write'),
              ContinuityStartRequest('seed-b', 'session-b', 'Ship step B', ()))
    listed = anyio.run(provider.list_open, _authorization(Op.CONTINUITY_LIST, 'read'), ContinuityListRequest())
    assert [item.session_id for item in listed.items] == ['session-b', 'session-a']
    assert listed.items[0].intent == 'Ship step B'
    bounded = anyio.run(provider.list_open, _authorization(Op.CONTINUITY_LIST, 'read'), ContinuityListRequest(1))
    assert [item.session_id for item in bounded.items] == ['session-b']


def test_a_note_named_for_its_task_carries_the_link_through_list_and_recover_on_brain_home():
    """N12b (Ciel slice 2): bc_continuity_start's task_id reaches the record, and a start that
    names no task binds the same input it bound before the field existed."""

    _, storage, clock = setup()
    provider = GitHubContinuityProvider(storage, clock)
    task_id = 'brn_20260928_110000_abcd1234'
    linked = ContinuityStartRequest('seed-a', 'call-the-bank', 'Call the bank', (), '', task_id)
    anyio.run(provider.start, _authorization(Op.CONTINUITY_START, 'write'), linked)
    plain = ContinuityStartRequest('seed-b', 'session-b', 'Ship step B', ())
    anyio.run(provider.start, _authorization(Op.CONTINUITY_START, 'write'), plain)

    listed = anyio.run(provider.list_open, _authorization(Op.CONTINUITY_LIST, 'read'), ContinuityListRequest())
    assert {item.session_id: item.task_id for item in listed.items} == {'call-the-bank': task_id, 'session-b': None}
    linked_note = next(item for item in listed.items if item.session_id == 'call-the-bank')
    assert linked_note.next_step == ''  # N12c: a note may start with no first step
    recovered = anyio.run(provider.recover, _authorization(Op.CONTINUITY_RECOVER, 'read'),
                          ContinuityRecoverRequest(2_500, 'call-the-bank'))
    assert recovered.task_id == task_id
    assert anyio.run(provider.recover, _authorization(Op.CONTINUITY_RECOVER, 'read'),
                     ContinuityRecoverRequest(2_500, 'session-b')).task_id is None

    # Replays: the same input returns the same note; a different task under the same key conflicts.
    again = anyio.run(provider.start, _authorization(Op.CONTINUITY_START, 'write'), linked)
    assert again.receipt.status == 'duplicate'
    assert anyio.run(provider.start, _authorization(Op.CONTINUITY_START, 'write'), plain).receipt.status == 'duplicate'
    with pytest.raises(BrainRuntimeError, match='different input'):
        anyio.run(provider.start, _authorization(Op.CONTINUITY_START, 'write'),
                  replace(linked, task_id='brn_20260928_110000_ffff'))


def test_snapshot_limit_fails_loudly_without_partial_search():
    _,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),_memory_request('limit-test','item'))
    from brain_core.github_storage import GitHubStorageError
    with pytest.raises(GitHubStorageError,match='explicit limit'):
        storage.snapshot_records(kinds=frozenset({'item'}),max_records=0)


# --- 2a: bc_memory_list (ROA-14) --------------------------------------------------


def test_list_sorts_by_due_date_then_recency_and_puts_undated_last():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    write=_authorization(Op.MEMORY_SAVE,'write')
    read=_authorization(Op.MEMORY_LIST,'read')

    due_today=_legacy_fixture(fake,provider,'list-today',title='Due today',data={'timing':{'due':'2026-09-21'}})
    due_tomorrow=_legacy_fixture(fake,provider,'list-tomorrow',title='Due tomorrow',data={'timing':{'due':'2026-09-22'}})
    undated_first=_legacy_fixture(fake,provider,'list-undated-1',title='Undated first')
    undated_second=_legacy_fixture(fake,provider,'list-undated-2',title='Undated second')

    listed=anyio.run(provider.list,read,MemoryListQuery())
    assert [item.record_id for item in listed.items]==[
        due_today.record_id,due_tomorrow.record_id,undated_second.record_id,undated_first.record_id,
    ]
    assert listed.version==fake.branch
    assert listed.freshness=='current'
    assert not listed.truncated
    assert listed.items[0].timing.get('due')
    assert listed.items[-1].timing=={}


def test_list_filters_by_word_state_level_and_due_before():
    import hashlib
    from gremlin_town.api.serialization import to_jsonable
    from brain_core.timewords import today_in_singapore
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    write=_authorization(Op.MEMORY_SAVE,'write')
    read=_authorization(Op.MEMORY_LIST,'read')

    task=_legacy_fixture(fake,provider,'list-task',title='A task',data={'timing':{'due':'2026-09-21'}})
    promise=_legacy_fixture(fake,provider,'list-promise',title='A promise',data={'owed_to':'Sam'})
    suggestion=_legacy_fixture(fake,provider,'list-suggestion',title='A suggestion',data={'level':'suggested'})
    _denied_change(fake,provider,_memory_change_request('list-wait',task.record_id,task.content_sha256))
    waiting=_legacy_fixture(fake,provider,'list-wait',title='A task',state='waiting',data={'timing':{'due':'2026-09-21'}},supersedes=(task.record_id,))

    default_listed=anyio.run(provider.list,read,MemoryListQuery())
    assert {item.record_id for item in default_listed.items}=={task.record_id,promise.record_id}
    current=next(item for item in default_listed.items if item.record_id==task.record_id)
    physical=_stored(provider,waiting.record_id)
    assert physical.record.id!=task.record_id and physical.record.supersedes==(task.record_id,)
    assert physical.record.state=='waiting'
    assert current.expected_sha256==hashlib.sha256(physical.content).hexdigest()

    by_word=anyio.run(provider.list,read,MemoryListQuery(word='promise'))
    assert [item.record_id for item in by_word.items]==[promise.record_id]
    assert to_jsonable(by_word.items[0])['word']=='promise'

    suggested=anyio.run(provider.list,read,MemoryListQuery(level='suggested'))
    assert [item.record_id for item in suggested.items]==[suggestion.record_id]

    only_waiting=anyio.run(provider.list,read,MemoryListQuery(state=('waiting',)))
    assert [item.record_id for item in only_waiting.items]==[task.record_id]

    today_str=today_in_singapore(FIRST_TIME).isoformat()
    due_filtered=anyio.run(provider.list,read,MemoryListQuery(due_before=today_str))
    assert [item.record_id for item in due_filtered.items]==[task.record_id]
    # The literal old wait has no recorded person; reading it does not authorize conversion.
    waiting_before=_stored(provider,waiting.record_id).record
    head,writes=fake.branch,len(fake.patch_payloads)
    with pytest.raises(BrainRuntimeError) as refused:
        _door(provider,WorkRequest('list-wait-door','A task needs Sam','owner','direct',
            ({'move':'wait','thing':waiting.record_id,'kind':'needs','who':'Sam'},)))
    assert refused.value.code=='brain.contract.invalid'
    assert refused.value.field=='data.timing.due'
    assert refused.value.fix=='correct the named field to the record contract'
    assert 'Nothing was saved' in str(refused.value)
    assert fake.branch==head and len(fake.patch_payloads)==writes
    assert _stored(provider,waiting.record_id).record==waiting_before
    # Separate synthetic native task proves the permitted wait without inventing old facts.
    native=_door(provider,WorkRequest('list-native-task','Synthetic task needs Sam','owner','direct',
        ({'move':'new','title':'Synthetic task'},)))
    door=_door(provider,WorkRequest('list-native-wait','Synthetic task needs Sam','owner','direct',
        ({'move':'wait','thing':native['things'][0]['record_id'],'kind':'needs','who':'Sam'},)))
    assert _stored(provider,door['things'][0]['record_id']).record.data['needs']=={'from':'Sam'}


def test_list_word_infers_level_but_an_explicit_level_overrides_it():
    """word='idea' alone must find level=possible items (plain_word only ever returns
    'idea' for level=possible), not silently return nothing under the outright
    level='accepted' default; passing level explicitly still filters exactly as given,
    so word='idea' with level='accepted' correctly finds nothing."""

    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    write=_authorization(Op.MEMORY_SAVE,'write')
    read=_authorization(Op.MEMORY_LIST,'read')

    idea=_legacy_fixture(fake,provider,'list-idea',title='An idea',data={'level':'possible'})

    by_word_alone=anyio.run(provider.list,read,MemoryListQuery(word='idea'))
    assert [item.record_id for item in by_word_alone.items]==[idea.record_id]

    by_word_with_explicit_accepted=anyio.run(provider.list,read,MemoryListQuery(word='idea',level='accepted'))
    assert not by_word_with_explicit_accepted.items


def test_list_takes_several_levels_in_one_call():
    """Speed-03: Diablo's "what's open?" block and bc_memory_list read accepted items and
    suggestions together, in one call; ideas stay out unless asked for."""

    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    write=_authorization(Op.MEMORY_SAVE,'write')
    read=_authorization(Op.MEMORY_LIST,'read')

    task=_legacy_fixture(fake,provider,'levels-task',title='A task')
    suggestion=_legacy_fixture(fake,provider,'levels-suggestion',title='A suggestion',data={'level':'suggested'})
    _legacy_fixture(fake,provider,'levels-idea',title='An idea',data={'level':'possible'})

    both=anyio.run(provider.list,read,MemoryListQuery(level=('accepted','suggested')))
    assert {item.record_id for item in both.items}=={task.record_id,suggestion.record_id}
    assert {item.level for item in both.items}=={'accepted','suggested'}


def test_list_sorts_and_filters_a_window_by_its_start():
    """Speed-03: "next week" is a window with no due date; it sorts among dated items by
    its start (as orientation reads it) instead of falling to the undated end."""

    fake,storage,clock=setup()  # FIRST_TIME is Monday 21 Sep
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    write=_authorization(Op.MEMORY_SAVE,'write')
    read=_authorization(Op.MEMORY_LIST,'read')

    later=_legacy_fixture(fake,provider,'window-oct',title='On 2 Oct',data={'timing':{'due':'2026-10-02'}})
    window=_legacy_fixture(fake,provider,'window-week',title='Next week',data={'timing':{'window':{'start':'2026-09-28','end':'2026-10-04'}}})
    friday=_legacy_fixture(fake,provider,'window-fri',title='Friday',data={'timing':{'due':'2026-09-25'}})
    undated=_legacy_fixture(fake,provider,'window-none',title='Undated')

    window_timing=_stored(provider,window.record_id).record.data['timing']
    assert window_timing['window']=={'start':'2026-09-28','end':'2026-10-04'} and 'due' not in window_timing
    listed=anyio.run(provider.list,read,MemoryListQuery())
    assert [item.record_id for item in listed.items]==[friday.record_id,window.record_id,later.record_id,undated.record_id]

    before=anyio.run(provider.list,read,MemoryListQuery(due_before='2026-09-30'))
    assert [item.record_id for item in before.items]==[friday.record_id,window.record_id]


def test_list_does_not_return_another_owner():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),_memory_request('list-private','item'))
    wrong=replace(_authorization(Op.MEMORY_LIST,'read'),owner_id='other-owner')
    assert not anyio.run(provider.list,wrong,MemoryListQuery()).items


def test_list_truncates_and_its_expected_sha256_enables_a_change_with_no_read():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    write=_authorization(Op.MEMORY_SAVE,'write')
    read=_authorization(Op.MEMORY_LIST,'read')
    for index in range(3):
        anyio.run(provider.save,write,replace(_memory_request(f'list-limit-{index}','item'),title=f'Task {index}'))
    bounded=anyio.run(provider.list,read,MemoryListQuery(limit=2))
    assert len(bounded.items)==2 and bounded.truncated
    listed_item=bounded.items[0]
    _denied_change(fake,provider,_memory_change_request('list-change-from-list',listed_item.record_id,
                                                      listed_item.expected_sha256))
    words=f'{listed_item.title} needs the fixture person'
    changed=_door(provider,WorkRequest('list-change-door',words,'owner','direct',
        ({'move':'wait','thing':listed_item.record_id,'kind':'needs','who':'fixture person'},)))
    assert changed['status']=='accepted'
    assert changed['things'][0]['record_id']!=listed_item.record_id
    assert _stored(provider,changed['things'][0]['record_id']).record.data['needs']=={'from':'fixture person'}


def test_list_costs_one_github_call_once_the_object_cache_is_warm():
    """Built on snapshot_records like _search (2a): with the object cache warm - the
    same mechanism that gets search under 1s after the copy (speed-01, 80c5ed7) - a
    second list against an unchanged branch costs exactly the branch-ref check."""

    fake=LANE_B.FakeGitHub()
    cache=GitHubObjectCache()
    storage=GitHubBrainHomeStorage(repository=LANE_B.REPOSITORY,branch=LANE_B.BRANCH,token=LANE_B.TOKEN,transport=fake,object_cache=cache)
    provider=GitHubBrainProvider(storage,_adapter(operation_time=lambda _: FIRST_TIME))

    anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),_memory_request('list-warm-1','item'))
    warm_up=anyio.run(provider.list,_authorization(Op.MEMORY_LIST,'read'),MemoryListQuery())
    assert len(warm_up.items)==1

    fake.events.clear()
    result=anyio.run(provider.list,_authorization(Op.MEMORY_LIST,'read'),MemoryListQuery())
    assert len(result.items)==1
    assert len(fake.events)==1 and fake.events[0].startswith('GET_REF')


# --- Memory follow-ons P1: confirmed preferences through memory.list ------------------


def _seeded_preference(suffix,*,title,timestamp,state='active',said_by='owner',supersedes=(),v1=False,owner='brain-home'):
    """A preference record as another writer (or an older schema) left it, straight from
    brain_core.records: the save tools only ever write active, owner-worded v2 preferences."""

    compact=timestamp.replace("-","").replace(":","").replace("T","_").split("+")[0]
    record_id=f"brn_{compact}_{suffix}"
    mapping={
        "schema":"brain.record/v1" if v1 else "brain.record/v2","id":record_id,"kind":"preference",
        "title":title,"state":state,"created_at":timestamp,"updated_at":timestamp,"sensitivity":"private",
        "vaults":["vault://brain-home"],"owners":[owner],"entities":[],
        "evidence":[{"source_type":"fixture","locator":"fixture:preference",
            "content_sha256":hashlib.sha256(title.encode()).hexdigest(),"excerpt":"owner words"}],
        "supersedes":list(supersedes),"dedupe_key":f"fixture:preference:{suffix}",
        "data":{"scope":"vault://brain-home","stability":"durable","evidence":["fixture:preference"],"correction_history":[]},
        "body":title,
    }
    if v1:
        mapping.update(assertion="explicit",confidence=1.0)
    else:
        mapping["source"]={"said_by":said_by,"came_by":"direct" if said_by=="owner" else "agent_guess",
            "words":"owner words","recorded_by":"brain-home",**({} if said_by=="owner" else {"speaker":"fixture-agent"})}
    record=record_from_mapping(mapping,"preference fixture")
    return f"records/preference/{compact[:4]}/{compact[4:6]}/{record_id}.md",render_record(record).encode("utf-8")


def test_list_of_preferences_is_nics_active_confirmed_preferences_newest_first():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    write=_authorization(Op.MEMORY_SAVE,'write')
    read=_authorization(Op.MEMORY_LIST,'read')
    older=anyio.run(provider.save,write,replace(_memory_request('pref-older','preference'),title='Short answers on WhatsApp'))
    newer=anyio.run(provider.save,write,replace(_memory_request('pref-newer','preference'),title='No calls before 9am'))
    task=anyio.run(provider.save,write,_memory_request('pref-task','item'))
    assert older.status==newer.status=='accepted'

    listed=anyio.run(provider.list,read,MemoryListQuery(kind='preference'))
    assert [item.record_id for item in listed.items]==[newer.record_id,older.record_id]
    first=listed.items[0]
    assert (first.title,first.state,first.word,first.level,first.timing,first.confirmed)==(
        'No calls before 9am','active',None,None,{},True)
    assert listed.version==task.version and not listed.truncated
    # Items stay items: the preference list never mixes in tasks, and the item list never shows preferences.
    assert [item.record_id for item in anyio.run(provider.list,read,MemoryListQuery()).items]==[task.record_id]

    bounded=anyio.run(provider.list,read,MemoryListQuery(kind='preference',limit=1))
    assert [item.record_id for item in bounded.items]==[newer.record_id] and bounded.truncated
    # word, state, level and due_before are item filters: with kind=preference they are not applied.
    filtered=anyio.run(provider.list,read,MemoryListQuery(kind='preference',word='task',state=('done',),
        level='suggested',due_before='2026-01-01'))
    assert [item.record_id for item in filtered.items]==[newer.record_id,older.record_id]
    with pytest.raises(BrainRuntimeError,match='kind must be item or preference'):
        anyio.run(provider.list,read,MemoryListQuery(kind='fact'))


def test_list_of_preferences_leaves_out_retired_replaced_unconfirmed_v1_and_other_owners():
    fake,storage,_=setup()
    kept_path,kept=_seeded_preference("a1a1a1a1",title="Kept",timestamp="2026-09-21T04:00:00+00:00")
    replaced_path,replaced=_seeded_preference("b2b2b2b2",title="Replaced",timestamp="2026-09-21T05:00:00+00:00")
    successor_path,successor=_seeded_preference("c3c3c3c3",title="Successor",timestamp="2026-09-21T06:00:00+00:00",
        supersedes=("brn_20260921_050000_b2b2b2b2",))
    seeded=dict([
        (kept_path,kept),(replaced_path,replaced),(successor_path,successor),
        _seeded_preference("d4d4d4d4",title="Retired",timestamp="2026-09-21T07:00:00+00:00",state="retired"),
        _seeded_preference("e5e5e5e5",title="Proposed",timestamp="2026-09-21T08:00:00+00:00",state="proposed"),
        _seeded_preference("f6f6f6f6",title="Guessed by an agent",timestamp="2026-09-21T09:00:00+00:00",said_by="agent"),
        _seeded_preference("a7a7a7a7",title="From the v1 era",timestamp="2026-09-21T10:00:00+00:00",v1=True),
        _seeded_preference("b8b8b8b8",title="Someone else's",timestamp="2026-09-21T11:00:00+00:00",owner="other-owner"),
    ])
    fake.seed(seeded)
    provider=GitHubBrainProvider(storage,_adapter(operation_time=lambda _:FIRST_TIME))
    listed=anyio.run(provider.list,_authorization(Op.MEMORY_LIST,'read'),MemoryListQuery(kind='preference'))
    assert [item.title for item in listed.items]==["Successor","Kept"]


def test_list_of_preferences_costs_one_github_call_once_the_object_cache_is_warm():
    """The same snapshot read as items (P1): Diablo's block asks for preferences on every
    turn it reads, so a warm copy must answer with the branch-ref check alone."""

    fake=LANE_B.FakeGitHub()
    cache=GitHubObjectCache()
    storage=GitHubBrainHomeStorage(repository=LANE_B.REPOSITORY,branch=LANE_B.BRANCH,token=LANE_B.TOKEN,transport=fake,object_cache=cache)
    provider=GitHubBrainProvider(storage,_adapter(operation_time=lambda _: FIRST_TIME))

    anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),_memory_request('pref-warm-1','preference'))
    warm_up=anyio.run(provider.list,_authorization(Op.MEMORY_LIST,'read'),MemoryListQuery(kind='preference'))
    assert len(warm_up.items)==1

    fake.events.clear()
    result=anyio.run(provider.list,_authorization(Op.MEMORY_LIST,'read'),MemoryListQuery(kind='preference'))
    assert len(result.items)==1
    assert len(fake.events)==1 and fake.events[0].startswith('GET_REF')


# --- 2b: the receipt is the proof -------------------------------------------------


def test_save_receipt_carries_state_and_level():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    write=_authorization(Op.MEMORY_SAVE,'write')
    saved=anyio.run(provider.save,write,_memory_request('receipt-save','item'))
    assert saved.state=='up' and saved.level=='accepted'
    replay=anyio.run(provider.save,write,_memory_request('receipt-save','item'))
    assert replay.status=='duplicate' and replay.state=='up' and replay.level=='accepted'


def test_change_receipt_carries_the_successor_record():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),_memory_request('receipt-change','item'))
    _denied_change(fake,provider,_memory_change_request('receipt-change-op',saved.record_id,
                                                      saved.content_sha256,state='waiting'))
    request=WorkRequest('receipt-change-door','Record title needs the fixture person','owner','direct',
        ({'move':'wait','thing':saved.record_id,'kind':'needs','who':'fixture person'},))
    changed=_door(provider,request)
    [thing]=changed['things']
    stored=_stored(provider,thing['record_id'])
    assert changed['status']=='accepted' and changed['version']==fake.branch
    assert thing['id']==saved.record_id and thing['record_id']!=saved.record_id
    assert thing['now']=='waiting' and stored.record.data['needs']=={'from':'fixture person'}
    assert stored.record.supersedes==(saved.record_id,)
    replay=_door(provider,request)
    assert replay=={**changed,'status':'duplicate'}


# --- Ciel rebuild slice 3 (ROA-65): importance (N5) and focus projects (N4) ---------------


def _as(subject,call,*args):
    """One provider call as an authenticated caller, as the MCP/HTTP tool boundary binds it."""

    from gremlin_town.mcp.host_identity import bind_write_attribution

    async def run():
        with bind_write_attribution(subject):
            return await call(*args)
    return anyio.run(run)


def _high(*,proposed_by,confirmed,reason='The Japan trip is off without it'):
    return {'level':'high','reason':reason,'proposed_by':proposed_by,'confirmed':confirmed}


def _agent(**changes):
    return {'said_by':'agent','came_by':'agent_guess','speaker':'damien','words':'That one matters',**changes}


def test_a_proposal_saves_unconfirmed_stamped_with_the_caller_and_shows_everywhere():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    request=replace(_memory_request('imp-propose','item'),title='Renew my passport',
        importance='proposed',importance_reason='The Japan trip is off without it')
    saved=_as('damien',provider.save,_authorization(Op.MEMORY_SAVE,'write'),request)
    assert saved.status=='accepted' and saved.importance is None
    assert {'importance','importance_reason'} <= set(saved.ignored) and saved.fix=='refresh the connector'
    replay=_as('damien',provider.save,_authorization(Op.MEMORY_SAVE,'write'),request)
    assert replay.status=='duplicate' and replay.importance is None
    listed=anyio.run(provider.list,_authorization(Op.MEMORY_LIST,'read'),MemoryListQuery())
    found=anyio.run(provider.search,_authorization(Op.MEMORY_SEARCH,'read'),MemorySearchQuery('passport'))
    assert [item.importance for item in listed.items]==[None]
    assert [match.importance for match in found.matches]==[None]
    read=anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),saved.record_id)
    assert read.importance is None and 'importance' not in read.canonical_record['data']
    proposed=_propose_stake(provider,'imp-door-propose')
    record=_stored(provider,proposed['things'][0]['record_id']).record
    assert record.data['stake']=={'level':'changes_life','at_stake':'The Japan trip is off without it',
                                  'proposed_by':'hermes-wa','confirmed':False}


def test_normal_items_keep_their_old_shape_and_show_no_importance():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('imp-normal','item'),importance='normal'))
    assert saved.importance is None
    record=anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),saved.record_id)
    assert 'importance' not in record.canonical_record['data'] and record.importance is None
    assert anyio.run(provider.list,_authorization(Op.MEMORY_LIST,'read'),MemoryListQuery()).items[0].importance is None


def test_an_agent_cannot_confirm_importance_but_nics_yes_does():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    proposed=_propose_stake(provider,'imp-confirm')
    saved=proposed['things'][0]['record_id']
    original=_stored(provider,saved)
    _denied_change(fake,provider,_memory_change_request('imp-self-confirm',saved,
        hashlib.sha256(original.content).hexdigest(),state=None,importance='confirmed',**_agent()),caller='damien')
    _denied_change(fake,provider,_memory_change_request('imp-yes',saved,hashlib.sha256(original.content).hexdigest(),
        state=None,importance='confirmed',said_by='owner',came_by='direct',speaker=None,words='yes, that one matters'),caller='chatgpt-web')
    guessed=_door(provider,WorkRequest('imp-agent-stake','That one matters','other','connected_source',
        ({'move':'stake','thing':saved,'confirm':True},),speaker='damien'))
    guess=_stored(provider,guessed['things'][0]['record_id']).record
    assert guess.data['stake']['confirmed'] is False
    words='Renew my passport matters because the Japan trip is off without it'
    yes=WorkRequest('imp-owner-stake',words,'owner','direct',
        ({'move':'stake','thing':guess.id,'confirm':True,'words':words},))
    confirmed=_door(provider,yes)
    actual=_stored(provider,confirmed['things'][0]['record_id']).record
    assert confirmed['status']=='accepted' and actual.data['stake']['confirmed'] is True
    assert actual.data['stake']['at_stake']=='The Japan trip is off without it'
    assert actual.data['stake']['proposed_by']=='hermes-wa'
    replay=_door(provider,yes)
    assert replay=={**confirmed,'status':'duplicate'}


def test_nic_can_state_importance_himself_but_an_agent_cannot_save_it_confirmed():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    for caller,source in (('chatgpt-web',{}),('damien',_agent())):
        saved=_as(caller,provider.save,_authorization(Op.MEMORY_SAVE,'write'),
            replace(_memory_request('imp-owner-'+caller,'item',**source),
                importance='confirmed',importance_reason='He loses the deposit'))
        assert saved.importance is None and saved.fix=='refresh the connector'
        assert {'importance','importance_reason'} <= set(saved.ignored)
        assert 'importance' not in _stored(provider,saved.record_id).record.data
    proposed=_propose_stake(provider,'imp-own-propose',reason='He loses the deposit')
    thing=proposed['things'][0]['record_id']
    words='Renew my passport is urgent, I lose the deposit if I miss it'
    confirmed=_door(provider,WorkRequest('imp-own-confirm',words,'owner','direct',
        ({'move':'stake','thing':thing,'confirm':True,'words':words},)))
    assert _stored(provider,confirmed['things'][0]['record_id']).record.data['stake']['confirmed'] is True


def test_only_nics_words_take_away_importance_he_confirmed():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=_legacy_fixture(fake,provider,'imp-remove',data={'importance':_high(proposed_by='damien',confirmed=True,reason='He loses the deposit')})
    original=anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),saved.record_id)
    assert original.importance['confirmed'] is True
    for label,change in (('normal',{'importance':'normal'}),('re-proposed',{'importance':'proposed','importance_reason':'Maybe not'})):
        _denied_change(fake,provider,_memory_change_request(f'imp-agent-{label}',saved.record_id,saved.content_sha256,
                                                          state=None,**change),caller='damien')
    _denied_change(fake,provider,_memory_change_request('imp-owner-normal',saved.record_id,saved.content_sha256,
        state=None,importance='normal',said_by='owner',came_by='direct',speaker=None,words="it's not that important any more"))
    words='Record title matters because he loses the deposit'
    confirmed=_door(provider,WorkRequest('imp-legacy-stake-confirm',words,'owner','direct',
        ({'move':'stake','thing':saved.record_id,'confirm':True,'words':words},)))
    confirmed_id=confirmed['things'][0]['record_id']
    assert _stored(provider,confirmed_id).record.data['stake']['confirmed'] is True
    unquoted=_door(provider,WorkRequest('imp-remove-no-quote',"it's not that important any more",'owner','direct',
        ({'move':'stake','thing':confirmed_id,'level':'nothing_much'},)))
    retained=_stored(provider,unquoted['things'][0]['record_id']).record
    assert retained.data['stake']['confirmed'] is True
    words="Record title: it's not that important any more"
    lowered=_door(provider,WorkRequest('imp-remove-owner',words,'owner','direct',
        ({'move':'stake','thing':retained.id,'level':'nothing_much','words':words},)))
    record=_stored(provider,lowered['things'][0]['record_id']).record
    assert lowered['status']=='accepted' and record.data['stake']['level']=='nothing_much'
    assert 'importance' not in record.data


def test_an_unconfirmed_proposal_can_be_restated_by_another_agent_as_its_own():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=_legacy_fixture(fake,provider,'imp-restate',data={'importance':_high(proposed_by='damien',confirmed=False)})
    original=anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),saved.record_id)
    assert original.importance==_high(proposed_by='damien',confirmed=False)
    _denied_change(fake,provider,_memory_change_request('imp-restated',saved.record_id,saved.content_sha256,state=None,
        importance='proposed',importance_reason='The visa appointment needs it first'),caller='chatgpt-web')
    words='Record title: The visa appointment needs it first'
    restated=_door(provider,WorkRequest('imp-restate-door',words,'owner','direct',
        ({'move':'stake','thing':saved.record_id,'level':'changes_life','at_stake':'The visa appointment needs it first'},)))
    assert _stored(provider,restated['things'][0]['record_id']).record.data['stake']=={
        'level':'changes_life','at_stake':'The visa appointment needs it first','proposed_by':'hermes-wa','confirmed':False}


@pytest.mark.parametrize(
    ('kind','fields','message'),
    [
        ('item',{'importance':'proposed'},'needs importance_reason'),
        ('item',{'importance':'confirmed'},'needs importance_reason'),
        ('item',{'importance_reason':'Why'},'goes only with importance proposed, confirmed, real_cost or minor'),
        ('item',{'importance':'normal','importance_reason':'Why'},'goes only with importance proposed, confirmed, real_cost or minor'),
        ('item',{'importance':'high','importance_reason':'Why'},'must be proposed, confirmed, real_cost, minor or normal'),
        ('fact',{'importance':'proposed','importance_reason':'Why'},'items only'),
        ('preference',{'importance_reason':'Why'},'items only'),
        ('item',{'focus_projects':('vault_exosuit',)},'for a preference only'),
        ('fact',{'focus_projects':('vault_exosuit',)},'for a preference only'),
    ],
)
def test_importance_and_focus_save_requests_are_refused_naming_the_fix(kind,fields,message):
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    before=len(fake.patch_payloads)
    if kind=='item' and any(field in fields for field in ('importance','importance_reason')):
        saved=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),replace(_memory_request(f'imp-bad-{kind}','item'),**fields))
        assert set(fields) <= set(saved.ignored) and saved.fix=='refresh the connector'
        assert saved.importance is None and 'importance' not in _stored(provider,saved.record_id).record.data
        return
    with pytest.raises(BrainRuntimeError,match=message) as refused:
        anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),replace(_memory_request(f'imp-bad-{kind}',kind),**fields))
    assert refused.value.code=='brain.invalid_argument'
    assert len(fake.patch_payloads)==before


def test_importance_change_requests_are_refused_naming_the_fix():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    write=_authorization(Op.MEMORY_SAVE,'write')
    item=anyio.run(provider.save,write,_memory_request('imp-plain','item'))
    fact=anyio.run(provider.save,write,_memory_request('imp-fact','fact'))
    for index,fields in enumerate(({'importance':'confirmed'},{'importance':'proposed'},{'importance_reason':'Why'})):
        _denied_change(fake,provider,_memory_change_request(f'imp-bad-change-{index}',item.record_id,item.content_sha256,
            state=None,said_by='owner',came_by='direct',speaker=None,words='yes',**fields))
    with pytest.raises(BrainRuntimeError,match='items only') as refused:
        anyio.run(provider.change,_authorization(Op.MEMORY_CHANGE,'write'),
            _memory_change_request('imp-bad-fact',fact.record_id,fact.content_sha256,state=None,
                importance='proposed',importance_reason='Why',said_by='owner',came_by='direct',speaker=None,words='yes'))
    assert refused.value.code=='brain.invalid_argument'


def test_focus_projects_save_only_in_nics_words_and_only_his_newest_list_is_listed():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    write=_authorization(Op.MEMORY_SAVE,'write')
    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(provider.save,write,replace(_memory_request('focus-agent','preference',**_agent()),focus_projects=('vault_exosuit',)))
    assert refused.value.code=='brain.record.needs_owner_words'

    first=anyio.run(provider.save,write,replace(_memory_request('focus-first','preference'),title='Focus projects',
        words='My focus is Exosuit',focus_projects=('vault_exosuit',)))
    ordinary=anyio.run(provider.save,write,replace(_memory_request('focus-ordinary','preference'),title='No calls before 9am'))
    newest=anyio.run(provider.save,write,replace(_memory_request('focus-newest','preference'),title='Focus projects',
        words='Focus on Exosuit and the job search now',focus_projects=('vault_exosuit','job-search')))
    assert first.status==ordinary.status==newest.status=='accepted'

    listed=anyio.run(provider.list,_authorization(Op.MEMORY_LIST,'read'),MemoryListQuery(kind='preference'))
    assert [item.record_id for item in listed.items]==[newest.record_id,ordinary.record_id]
    read=anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),newest.record_id)
    assert read.canonical_record['data']['focus_projects']==['vault_exosuit','job-search']
    assert read.canonical_record['source']['said_by']=='owner'


# --- Release ciel-03 slice 1 (ROA-75): guessed stakes, block, rough length and raise-with ---------


def test_an_agents_guessed_stake_block_length_and_person_save_as_its_guesses():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    legacy=_legacy_fixture(fake,provider,'c3-old-guess',data={'importance':{'level':'real_cost','reason':'A S$40 late fee',
        'proposed_by':'damien','confirmed':False},'block':'admin','raise_with':'Sam',
        'timing':{'duration_minutes':10,'padded_minutes':15,'estimated_by':'damien'}})
    read=anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),legacy.record_id)
    assert read.importance=={'level':'real_cost','reason':'A S$40 late fee','proposed_by':'damien','confirmed':False}
    data=read.canonical_record['data']
    assert (data['block'],data['raise_with'])==('admin','Sam')
    length={field:data['timing'][field] for field in ('duration_minutes','padded_minutes','estimated_by')}
    assert length=={'duration_minutes':10,'padded_minutes':15,'estimated_by':'damien'}
    listed=anyio.run(provider.list,_authorization(Op.MEMORY_LIST,'read'),MemoryListQuery())
    assert [(item.block,item.raise_with) for item in listed.items]==[('admin','Sam')]
    saved=_as('damien',provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('c3-guess','item'),importance='real_cost',importance_reason='A S$40 late fee',
            block='admin',rough_minutes=10,raise_with='Sam'))
    assert saved.status=='accepted' and saved.importance is None
    assert {'importance','importance_reason'} <= set(saved.ignored) and saved.fix=='refresh the connector'
    assert 'importance' not in _stored(provider,saved.record_id).record.data
    assert _stored(provider,saved.record_id).record.data['timing']==length


def test_items_without_the_new_fields_list_them_as_none():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),_memory_request('c3-plain','item'))
    [item]=anyio.run(provider.list,_authorization(Op.MEMORY_LIST,'read'),MemoryListQuery()).items
    assert (item.block,item.raise_with)==(None,None)


@pytest.mark.parametrize('setting',['real_cost','minor','proposed'])
def test_a_stake_needs_its_reason(setting):
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=_as('damien',provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('c3-noreason','item'),importance=setting))
    assert 'importance' in saved.ignored and saved.fix=='refresh the connector'
    assert saved.importance is None and 'importance' not in _stored(provider,saved.record_id).record.data


@pytest.mark.parametrize('field,value',[('block','admin'),('raise_with','Sam'),('rough_minutes',10)])
def test_the_sorting_fields_are_for_items_only(field,value):
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),replace(_memory_request('c3-fact','fact'),**{field:value}))
    assert refused.value.code=='brain.invalid_argument'


def test_an_agent_adds_a_rough_length_with_no_time_words_and_new_dates_keep_it():
    from brain_core.language import check_change
    from brain_core.records import parse_record

    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=_legacy_fixture(fake,provider,'c3-len',data={'timing':{'due':'2026-09-25'}})
    due=_stored(provider,saved.record_id).record.data['timing']['due']
    original=anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),saved.record_id)
    assert original.canonical_record['data']['timing']=={'due':due}
    _denied_change(fake,provider,_memory_change_request('c3-len-add',saved.record_id,saved.content_sha256,state=None,
        rough_minutes=120,block='deep_focus',**_agent(words='That will take a couple of hours')),caller='damien')
    words='Record title takes a couple of hours in deep focus'
    before=_stored(provider,saved.record_id).record
    head,writes=fake.branch,len(fake.patch_payloads)
    carried=_door(provider,WorkRequest('c3-len-missing-provenance',words,'owner','direct',
        ({'move':'organise','thing':saved.record_id,'rough_minutes':120,'block':'deep_focus','words':words},)))
    after=_stored(provider,carried['things'][0]['record_id']).record
    assert carried['status']=='accepted' and fake.branch!=head and len(fake.patch_payloads)==writes+1
    assert after.data['timing']=={'due':due,'duration_minutes':120,'padded_minutes':180,'estimated_by':'hermes-wa'}
    assert after.data['block']=='deep_focus' and 'dates' not in after.data
    assert 'resolved_from' not in after.data['timing']
    assert {key:value for key,value in after.data.items() if key not in ('timing','block')}=={
        key:value for key,value in before.data.items() if key!='timing'}
    assert (after.title,after.body,after.owners,after.vaults)==(before.title,before.body,before.owners,before.vaults)
    assert after.supersedes==(before.id,) and after.id!=before.id
    assert after.source=={'said_by':'owner','came_by':'direct','recorded_by':'hermes-wa','words':words}
    # The authenticated envelope fills only absent evidence actors; existing
    # evidence content and any supplied authors remain intact.
    assert after.evidence[:-1]==tuple(replace(e,actor=e.actor or 'hermes-wa') for e in before.evidence)
    assert after.evidence[-1].excerpt==words and after.evidence[-1].actor=='hermes-wa'
    roundtrip=parse_record(render_record(after),'unchanged legacy date carry')
    assert roundtrip==after
    check_change(before,roundtrip,actor='hermes-wa',message=words)
    read=anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),after.id)
    assert read.canonical_record['data']['timing']==after.data['timing']
    assert read.canonical_record['data']['timing']['due']==original.canonical_record['data']['timing']['due']
    assert _stored(provider,saved.record_id).record.data['timing']=={'due':due}
    # This separate synthetic legacy row has actual fixture source words for its date.
    date_words='due 2026-09-25'
    saved=_legacy_fixture(fake,provider,'c3-len-representable',
        body='Record title is '+date_words,
        data={'timing':{'due':due,'resolved_from':{'words':date_words,'today':'2026-09-21'}}})
    represented=_stored(provider,saved.record_id).record
    assert represented.source['words']==represented.body=='Record title is '+date_words
    assert represented.evidence[0].excerpt==represented.body
    assert represented.data['timing']['resolved_from']=={'words':date_words,'today':'2026-09-21'}
    assert represented.created_at.astimezone(SINGAPORE).date().isoformat()=='2026-09-21'
    lengthened=_door(provider,WorkRequest('c3-len-door',words,'owner','direct',
        ({'move':'organise','thing':saved.record_id,'rough_minutes':120,'block':'deep_focus','words':words},)))
    current=lengthened['things'][0]['record_id']
    actual=_stored(provider,current).record
    assert lengthened['status']=='accepted' and actual.data['dates']['deadline']['day']==due
    assert _nics_length(current,provider)==(120,180,'hermes-wa')
    _denied_change(fake,provider,_memory_change_request('c3-len-move',current,
        hashlib.sha256(_stored(provider,current).content).hexdigest(),state=None,time_words='Monday',
        **_agent(words='Nic moved it to Monday')),caller='damien')
    moved=_door(provider,WorkRequest('c3-len-move-door','Record title is due Monday','owner','direct',
        ({'move':'date','thing':current,'when':'due Monday'},)))
    changed=_stored(provider,moved['things'][0]['record_id']).record
    assert changed.data['dates']['deadline']['day']!=due
    assert _nics_length(changed.id,provider)==(120,180,'hermes-wa')


def test_a_new_date_reading_without_owner_date_words_is_dropped_as_one_question():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    words='Record title takes a couple of hours in deep focus'
    created=_door(provider,WorkRequest('c3-date-guard-new',words,'owner','direct',
        ({'move':'new','title':'Record title'},)))
    before=_stored(provider,created['things'][0]['record_id']).record
    writes=len(fake.patch_payloads)
    dropped=_door(provider,WorkRequest('c3-date-guard',words,'owner','direct',
        ({'move':'date','thing':before.id,'when':'Monday',
          'reading':{'kind':'planned','day':'2026-09-28'}},)))
    after=_stored(provider,dropped['things'][0]['record_id']).record
    assert dropped['status']=='accepted' and len(fake.patch_payloads)==writes+1
    assert dropped['things'][0]['dates']==[] and dropped['things'][0]['flags']==[{
        'move':0,'thing':before.id,'field':'when','message':'date left off: phrase not in his words',
        'dropped':True,'words':words}]
    assert 'dates' not in after.data and after.data['timing']==before.data['timing']=={}
    assert after.data['review_marks']==[{'mark':'question',
        'text':words+' — date: date left off: phrase not in his words'}]
    assert {key:value for key,value in after.data.items() if key!='review_marks'}==before.data
    assert after.supersedes==(before.id,) and after.body==before.body
    assert after.source['words']==words and after.source['recorded_by']=='hermes-wa'


def test_nics_yes_turns_a_guessed_stake_into_cant_undo():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=_legacy_fixture(fake,provider,'c3-yes',data={'importance':{
        'level':'real_cost','reason':'We lose the deposit','proposed_by':'damien','confirmed':False}})
    _denied_change(fake,provider,_memory_change_request('c3-yes-confirm',saved.record_id,saved.content_sha256,state=None,
        importance='confirmed',said_by='owner',came_by='direct',speaker=None,words='yes, that can not be undone'),caller='chatgpt-web')
    words='Record title: We lose the deposit, that cannot be undone'
    confirmed=_door(provider,WorkRequest('c3-stake-confirm',words,'owner','direct',
        ({'move':'stake','thing':saved.record_id,'level':'changes_life','at_stake':'We lose the deposit',
          'confirm':True,'words':words},)))
    assert _stored(provider,confirmed['things'][0]['record_id']).record.data['stake']=={
        'level':'changes_life','at_stake':'We lose the deposit','proposed_by':'hermes-wa','confirmed':True}


# --- Release ciel-03 slice 4 (ROA-78): sorting new items once, with each outcome's parts -----------


_NEXT_ACTION = dict(sort='next_action', block='admin', first_step="Find the accountant's number", rough_minutes=15)


def test_a_next_action_without_its_block_type_is_refused_and_nothing_is_saved():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    half={key:value for key,value in _NEXT_ACTION.items() if key!='block'}
    saved=_as('damien',provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('c4-half','item',level='suggested',**_agent(words='call the accountant about the VAT')),**half))
    assert {'sort','first_step'} <= set(saved.ignored) and saved.fix=='refresh the connector'
    raw=_stored(provider,saved.record_id).record
    assert 'block' not in raw.data and 'next_action' not in raw.data and 'first_step' not in raw.data
    head,writes=fake.branch,len(fake.patch_payloads)
    with pytest.raises(BrainRuntimeError) as refused:
        _door(provider,WorkRequest('c4-half-door','Call the accountant about the VAT','owner','direct',
            ({'move':'next_action','thing':saved.record_id},)))
    assert refused.value.code=='brain.invalid_argument' and refused.value.field=='text' and refused.value.fix
    assert fake.branch==head and len(fake.patch_payloads)==writes
    listed=anyio.run(provider.list,_authorization(Op.MEMORY_LIST,'read'),MemoryListQuery(level=['accepted','suggested']))
    assert [item.record_id for item in listed.items]==[saved.record_id]


def test_a_pre_sorted_next_action_saves_with_its_first_step_and_the_sort_is_never_stored():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    old=_legacy_fixture(fake,provider,'c4-old-vat',title='Call the accountant about the VAT',data={
        'level':'suggested','block':'admin','first_step':"Find the accountant's number",
        'timing':{'duration_minutes':15,'padded_minutes':23,'estimated_by':'damien'}})
    data=anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),old.record_id).canonical_record['data']
    assert (data['level'],data['block'],data['first_step'])==('suggested','admin',"Find the accountant's number")
    assert data['timing']['duration_minutes']==15 and 'sort' not in data and 'sort_input' not in data
    [listed]=anyio.run(provider.list,_authorization(Op.MEMORY_LIST,'read'),MemoryListQuery(level='suggested')).items
    assert (listed.block,listed.first_step)==('admin',"Find the accountant's number")
    saved=_as('damien',provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('c4-vat','item',level='suggested',title='Call the accountant about the VAT',
            **_agent(words='call the accountant about the VAT')),**_NEXT_ACTION))
    assert saved.status=='accepted' and {'sort','first_step'} <= set(saved.ignored)
    assert saved.fix=='refresh the connector' and 'first_step' not in _stored(provider,saved.record_id).record.data
    yes=_memory_change_request('c4-vat-yes',saved.record_id,saved.content_sha256,state=None,level='accepted',
        sort='next_action',said_by='owner',came_by='direct',speaker=None,words='yes, sort it like that')
    _denied_change(fake,provider,yes)
    words="Call the accountant about the VAT: accept it. Find the accountant's number"
    accepted=_door(provider,WorkRequest('c4-vat-door',words,'owner','direct',(
        {'move':'organise','thing':saved.record_id,'accept':True,'words':words},
        {'move':'next_action','thing':saved.record_id,'text':"Find the accountant's number"})))
    actual=_stored(provider,accepted['things'][0]['record_id']).record
    assert accepted['status']=='accepted' and actual.data['level']=='accepted'
    assert actual.data['next_action']=={'text':"Find the accountant's number",'done':False}


def test_waiting_on_saves_the_item_waiting_and_needs_who():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    old=_legacy_fixture(fake,provider,'c4-legacy-wait',state='waiting',links=({'type':'waiting_on','target':'Sam'},))
    assert anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),old.record_id).canonical_record['state']=='waiting'
    saved=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        _memory_request('c4-wait','item',sort='waiting_on',links=({'type':'waiting_on','target':'Sam'},)))
    assert {'sort','links'} <= set(saved.ignored) and saved.fix=='refresh the connector'
    raw=_stored(provider,saved.record_id).record
    assert raw.state=='open' and raw.links==() and 'needs' not in raw.data
    head,writes=fake.branch,len(fake.patch_payloads)
    with pytest.raises(BrainRuntimeError) as refused:
        _door(provider,WorkRequest('c4-wait-who','Record title needs someone','owner','direct',
            ({'move':'wait','thing':saved.record_id,'kind':'needs'},)))
    assert refused.value.code=='brain.invalid_argument' and refused.value.field=='who' and refused.value.fix
    assert fake.branch==head and len(fake.patch_payloads)==writes
    actual=_door(provider,WorkRequest('c4-wait-door','Record title needs Sam','owner','direct',
        ({'move':'wait','thing':saved.record_id,'kind':'needs','who':'Sam'},)))
    assert actual['things'][0]['now']=='waiting'
    assert _stored(provider,actual['things'][0]['record_id']).record.data['needs']=={'from':'Sam'}


@pytest.mark.parametrize('kind,fields,message',[
    ('item',{'sort':'drop'},'sort=drop: nothing to save'),
    ('preference',{'sort':'reference'},'sort is for items and facts only'),
    ('fact',{'first_step':'Look it up'},'block, raise_with and first_step are for items only'),
    ('item',{'sort':'someday'},'level possible'),
    ('item',{'sort':'raise_with'},'raise_with \(who to raise it with\)'),
])
def test_a_sort_that_does_not_fit_is_refused(kind,fields,message):
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    if kind=='item':
        saved=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),_memory_request('c4-misfit',kind,**fields))
        assert 'sort' in saved.ignored and saved.fix=='refresh the connector'
        assert 'sort' not in _stored(provider,saved.record_id).record.data
        return
    with pytest.raises(BrainRuntimeError,match=message) as refused:
        anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),_memory_request('c4-misfit',kind,**fields))
    assert refused.value.code=='brain.invalid_argument'


def test_a_fact_sorted_as_reference_and_an_idea_sorted_as_someday_save():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    fact=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),_memory_request('c4-ref','fact',sort='reference'))
    idea=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        _memory_request('c4-idea','item',level='possible',sort='someday'))
    assert fact.status==idea.status=='accepted'


def test_sorting_an_unsorted_task_checks_the_whole_changed_record():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),_memory_request('c4-old','item'))
    no_step=_memory_change_request('c4-old-half',saved.record_id,saved.content_sha256,state=None,sort='next_action',
        block='errands',rough_minutes=30,**_agent(words='Diablo pre-sorted it; Nic said yes'))
    _denied_change(fake,provider,no_step,caller='damien')
    _denied_change(fake,provider,replace(no_step,operation_id='c4-old-sorted',first_step='Check the shop hours'),caller='damien')
    words='Record title is errands: Check the shop hours, about 30 minutes'
    sorted_=_door(provider,WorkRequest('c4-old-door',words,'owner','direct',(
        {'move':'organise','thing':saved.record_id,'block':'errands','rough_minutes':30,'words':words},
        {'move':'next_action','thing':saved.record_id,'text':'Check the shop hours'})))
    data=_stored(provider,sorted_['things'][0]['record_id']).record.data
    assert (data['block'],data['next_action']['text'],data['timing']['padded_minutes'])==('errands','Check the shop hours',45)


# --- Release ciel-03 slice 7 (ROA-81): a whole project saved at once, all or nothing -------------


def _plan(**changes):
    parts = changes.pop('parts', [
        {'title': 'Buy boxes', 'block': 'errands', 'rough_minutes': 45},
        {'title': 'Pack the books', 'block': 'admin', 'rough_minutes': 120, 'then': True},
        {'title': 'Book the movers', 'block': 'admin', 'rough_minutes': 20},
    ])
    message = ('Yes, save the Move house goal: Out of the flat by the end of the month. '
               'Buy boxes, check the hardware shop hours, 45 minutes of errands. '
               'Then Pack the books, 120 minutes of admin. Book the movers, get three quotes, '
               '20 minutes of admin. I want it done by Friday.')
    values = dict(operation_id='c7-move', message=message, said_by='owner', came_by='direct',
                  moves=({'move': 'new', 'title': 'Move house'},
                         {'move': 'organise', 'thing': '#1', 'called': 'goal',
                          'finish_line': 'Out of the flat by the end of the month', 'words': message},
                         {'move': 'parts', 'thing': '#1', 'parts': parts}))
    values.update(changes)
    return WorkRequest(**values)


def _door(provider, request):
    return _as('hermes-wa', provider.work, _authorization(Op.WORK, 'write'), request)


def _stored(provider, record_id):
    return next(item for item in provider._storage.snapshot_records(kinds=frozenset({'item'})).records
                if item.record.id == record_id)


def _legacy_fixture(fake,provider,operation_id,*,title='Record title',body='Validated item body',
                    state='open',data=None,links=(),supersedes=()):
    """Typed literal old data proves unchanged reader behavior without a retired writer."""
    at=FIRST_TIME+timedelta(seconds=len(fake.commits)-1)
    record_id='brn_'+at.astimezone(UTC).strftime('%Y%m%d_%H%M%S')+'_'+hashlib.sha256(operation_id.encode()).hexdigest()[:16]
    record=record_from_mapping({
        'schema':'brain.record/v2','id':record_id,'kind':'item','title':title,'body':body,
        'state':state,'created_at':at.isoformat(),'updated_at':at.isoformat(),
        'sensitivity':'private','vaults':['vault://brain-home'],'owners':['brain-home'],'entities':[],
        'evidence':[{'source_type':'fixture','locator':'fixture:'+operation_id,
                     'content_sha256':hashlib.sha256(body.encode()).hexdigest(),'excerpt':body}],
        'supersedes':list(supersedes),'dedupe_key':'fixture:'+operation_id,
        'data':{'level':'accepted','size':'task',**(data or {})},'links':list(links),
        'source':{'said_by':'owner','came_by':'direct','words':body,'recorded_by':'damien'},
    },'legacy item fixture')
    content=render_record(record).encode()
    path=f'records/item/2026/09/{record_id}.md'
    fake.seed({path:content})
    provider._records.remember_record(_authorization(Op.MEMORY_READ,'read'),record_id,path)
    return SimpleNamespace(record_id=record_id,content_sha256=hashlib.sha256(content).hexdigest())


def _propose_stake(provider,operation_id,*,title='Renew my passport',reason='The Japan trip is off without it'):
    return _door(provider,WorkRequest(operation_id,f'{title}. {reason}','owner','direct',
        ({'move':'new','title':title,'stake':{'level':'changes_life','at_stake':reason}},)))


def _maybe_same(provider,record_id):
    return tuple(mark['as'] for mark in _stored(provider,record_id).record.data.get('review_marks',())
                 if mark['mark']=='maybe_same')


def _denied_change(fake, provider, request, *, caller='hermes-wa'):
    head, writes = fake.branch, len(fake.patch_payloads)
    with pytest.raises(BrainRuntimeError) as refused:
        _as(caller, provider.change, _authorization(Op.MEMORY_CHANGE, 'write'), request)
    assert refused.value.code == 'ACCESS_DENIED'
    assert refused.value.fix == ('use bc_work' if caller == 'hermes-wa' else
        "save it as a new thing with memory.save; Nic's morning review merges it")
    assert fake.branch == head and len(fake.patch_payloads) == writes
    return refused.value


def test_plan_the_move_saves_the_goal_and_its_steps_with_what_blocks_what_in_one_commit():
    """Acceptance case 17: "plan the move" is saved as its steps, with what blocks what, after one
    approval, in one commit, with a receipt per record."""

    from test_github_links_at_newest import _plan_saved as legacy_plan

    _, provider, receipt = legacy_plan()
    goal,boxes,pack,movers=(receipt.goal,*receipt.steps)
    read=lambda record_id: anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),record_id).canonical_record
    goal_record=read(goal.record_id)
    assert (goal_record['data']['size'],goal_record['data']['level'])==('goal','accepted')
    assert 'due' not in (goal_record['data'].get('timing') or {})  # "by Friday" in his yes is no step's date
    pack_record=read(pack.record_id)
    assert pack_record['state']=='blocked'
    assert pack_record['links']==[{'type':'part_of','target':goal.record_id},{'type':'blocked_by','target':boxes.record_id}]
    boxes_record=read(boxes.record_id)
    assert (boxes_record['state'],boxes_record['data']['first_step'],boxes_record['data']['block'])==(
        'open','Check the hardware shop hours','errands')
    assert boxes_record['links']==[{'type':'part_of','target':goal.record_id}]

    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    commits_before=len([event for event in fake.events if event.startswith('POST_COMMIT')])
    receipt=_door(provider,_plan())
    assert receipt['status']=='accepted' and receipt['version']==fake.branch
    assert len([event for event in fake.events if event.startswith('POST_COMMIT')])==commits_before+1
    actual=[item.record for item in provider._storage.snapshot_records(kinds=frozenset({'item'})).records]
    assert len(actual)==4 and len(receipt['things'])==3
    assert '0 of 3' in receipt['say'] and 'and 1 more' in receipt['say']
    assert {thing['record_id'] for thing in receipt['things']} <= {record.id for record in actual}
    goal=next(record for record in actual if 'parent' not in record.data)
    boxes,pack,movers=sorted((record for record in actual if record.data.get('parent')==goal.id),
                            key=lambda record: record.data['order'])
    assert len(actual)==4 and goal.data['called']=='goal'
    assert 'deadline' not in goal.data.get('dates',{})
    assert pack.data['after']==[boxes.id]
    assert all(record.data['parent']==goal.id for record in (boxes,pack,movers))
    assert (boxes.data['block'],boxes.data['timing']['duration_minutes'])==('errands',45)
    assert receipt['things'] and receipt['say']
    again=_door(provider,_plan())
    assert again=={**receipt,'status':'duplicate'}
    assert len([event for event in fake.events if event.startswith('POST_COMMIT')])==commits_before+1


def test_a_step_missing_a_part_refuses_the_whole_plan_and_nothing_is_saved():
    from test_brain_runtime import runtime

    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    legacy=runtime(memory=provider)
    head,writes=fake.branch,len(fake.patch_payloads)
    with pytest.raises(BrainRuntimeError) as retired:
        anyio.run(legacy.save_project,BrainCaller('hermes-wa',frozenset({'bc:write'})),OwnerSelector('personal'),_plan())
    assert retired.value.code=='ACCESS_DENIED' and retired.value.fix=='use bc_work'
    assert fake.branch==head and len(fake.patch_payloads)==writes
    for index,part in enumerate(({'block':'errands','rough_minutes':45},{'title':123})):
        with pytest.raises(BrainRuntimeError) as malformed:
            _door(provider,_plan(operation_id=f'c7-malformed-{index}',parts=[part]))
        assert malformed.value.code=='brain.invalid_argument' and malformed.value.field and malformed.value.fix
        assert fake.branch==head and len(fake.patch_payloads)==writes
    listed=anyio.run(provider.list,_authorization(Op.MEMORY_LIST,'read'),MemoryListQuery(state=['open','blocked']))
    assert listed.items==()
    valid=_door(provider,_plan(operation_id='c7-no-first-step',parts=[{'title':'Buy boxes','block':'errands','rough_minutes':45}]))
    assert valid['status']=='accepted'
    child=next(item.record for item in provider._storage.snapshot_records(kinds=frozenset({'item'})).records
               if item.record.data.get('parent'))
    assert child.title=='Buy boxes' and 'next_action' not in child.data
    assert any(thing['record_id']==child.id and thing['next']['text']=='Buy boxes' for thing in valid['things'])


@pytest.mark.parametrize('blocked_by,message',[
    (((2,),(1,)),'in a loop'),
    (((1,),()),'never itself'),
    (((3,),()),'steps 1 to 2'),
])
def test_steps_that_wait_on_each_other_wrongly_are_refused(blocked_by,message):
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    parts=[{'title':f'Step {number}','block':'admin','rough_minutes':10}
           for number in range(1,len(blocked_by)+1)]
    change=_plan(parts=parts)
    waits=tuple({'move':'wait','thing':f'Step {number}','kind':'after',
                 'after':[f'Step {target}' for target in targets]}
                for number,targets in enumerate(blocked_by,start=1) if targets)
    change=replace(change,moves=(*change.moves,*waits))
    head,writes=fake.branch,len(fake.patch_payloads)
    with pytest.raises(BrainRuntimeError) as refused:
        _door(provider,change)
    assert refused.value.field and refused.value.fix
    if message=='never itself':
        assert refused.value.code=='brain.contract.invalid' and refused.value.field=='data.after'
    elif message=='in a loop':
        assert refused.value.code=='brain.invalid_argument' and 'loop' in str(refused.value)
    else:
        assert refused.value.code=='brain.invalid_argument' and 'Step 3' in str(refused.value)
    assert fake.branch==head and len(fake.patch_payloads)==writes


def test_a_project_needs_nics_own_yes():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    proposed=_door(provider,_plan(said_by='other',came_by='connected_source',speaker='chatgpt-web',message='Here is a plan'))
    actual=[item.record for item in provider._storage.snapshot_records(kinds=frozenset({'item'})).records]
    assert proposed['status']=='accepted' and len(actual)==1
    assert actual[0].data['level']=='suggested' and 'called' not in actual[0].data
    assert any(flag.get('dropped') for thing in proposed['things'] for flag in thing['flags'])
    accepted=_door(provider,_plan(operation_id='c7-owner-plan'))
    current=[item.record for item in provider._storage.snapshot_records(kinds=frozenset({'item'})).records
             if item.record.source['recorded_by']=='hermes-wa' and item.record.data['level']=='accepted']
    assert accepted['status']=='accepted' and len(current)==4
    assert any(record.data.get('called')=='goal' for record in current)


def test_a_lost_reply_saves_nothing_and_the_same_input_again_saves_it_all():
    """Accepted - Nic, 28 Sep 2026 ("Nothing saved; retry")."""

    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    fake.lost_reject_count=1
    with pytest.raises(BrainRuntimeError):
        _door(provider,_plan())
    assert anyio.run(provider.list,_authorization(Op.MEMORY_LIST,'read'),MemoryListQuery(state=['open','blocked'])).items==()
    receipt=_door(provider,_plan())
    assert receipt['status']=='accepted' and len(receipt['things'])==3
    assert '0 of 3' in receipt['say'] and 'and 1 more' in receipt['say']
    assert len(provider._storage.snapshot_records(kinds=frozenset({'item'})).records)==4


# --- Release ciel-03 slice 8 (ROA-82): a routine's occurrences --------------------------------------


def _routine(provider):
    return anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        _memory_request('c8-laundry','item',title='Laundry',repeat='daily',words='I do the laundry every day'))


def test_nics_did_it_records_the_routines_occurrence_and_it_stays_open():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=_routine(provider)
    request=_memory_change_request('c8-done',saved.record_id,saved.content_sha256,state=None,occurrence='done',
        said_by='owner',came_by='direct',speaker=None,words='did the laundry')
    _denied_change(fake,provider,request)
    words='I did the laundry'
    done=_door(provider,WorkRequest('c8-tick',words,'owner','direct',
        ({'move':'tick','thing':saved.record_id,'what':'thing','words':words},)))
    assert done['status']=='accepted'
    record=anyio.run(provider.read,_authorization(Op.MEMORY_READ,'read'),done['things'][0]['record_id']).canonical_record
    assert record['state']=='open'
    day=FIRST_TIME.astimezone(SINGAPORE).date().isoformat()
    assert record['data']['occurrence']=={'day':day,'outcome':'done'}
    assert saved.record_id in record['supersedes']


def test_only_nics_word_records_an_occurrence_and_only_on_a_routine():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=_routine(provider)
    _denied_change(fake,provider,_memory_change_request('c8-agent',saved.record_id,saved.content_sha256,
                                                      state=None,occurrence='done'))
    agent=_door(provider,WorkRequest('c8-agent-door','Nic did the laundry','other','connected_source',
        ({'move':'tick','thing':saved.record_id,'what':'thing'},),speaker='damien'))
    untouched=_stored(provider,agent['things'][0]['record_id']).record
    assert untouched.state=='open' and 'occurrence' not in untouched.data
    assert any(flag.get('dropped') for thing in agent['things'] for flag in thing['flags'])
    task=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),_memory_request('c8-task','item'))
    _denied_change(fake,provider,_memory_change_request('c8-task-done',task.record_id,task.content_sha256,state=None,
        occurrence='done',said_by='owner',came_by='direct',speaker=None,words='done'))
    words='I finished Record title'
    done=_door(provider,WorkRequest('c8-task-tick',words,'owner','direct',
        ({'move':'tick','thing':task.record_id,'what':'thing','words':words},)))
    actual=_stored(provider,done['things'][0]['record_id']).record
    assert actual.state=='done' and 'occurrence' not in actual.data


def test_a_routine_itself_is_never_marked_done():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=_routine(provider)
    _denied_change(fake,provider,_memory_change_request('c8-closed',saved.record_id,saved.content_sha256,state='done',
        said_by='owner',came_by='direct',speaker=None,words='did it',check_locator='his word',checked_by='nic'))
    words='I did the laundry'
    done=_door(provider,WorkRequest('c8-closed-door',words,'owner','direct',
        ({'move':'tick','thing':saved.record_id,'what':'thing','words':words},)))
    assert _stored(provider,done['things'][0]['record_id']).record.state=='open'
    assert _stored(provider,done['things'][0]['record_id']).record.data['occurrence']['outcome']=='done'


# --- Step I §6 (placement engine slice 1): Nic's own length is booked as he said it ---------------


def _nics_length(record_id, provider):
    timing=_stored(provider,record_id).record.data['timing']
    return (timing['duration_minutes'],timing['padded_minutes'],timing['estimated_by'])


def test_a_length_nic_gives_on_save_is_booked_unpadded():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=_as('damien',provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('pl-own','item'),words='Call the bank, that takes an hour',
            block='admin',rough_minutes=60,length_is_nics=True))
    assert saved.status=='accepted'
    assert _nics_length(saved.record_id,provider)==(60,60,'nic')


def test_nics_correction_replaces_an_agents_guess_unpadded_and_keeps_the_history():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    guessed=_as('damien',provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('pl-fix','item'),block='admin',rough_minutes=30))
    assert _nics_length(guessed.record_id,provider)==(30,45,'damien')
    _denied_change(fake,provider,_memory_change_request('pl-fix-nic',guessed.record_id,guessed.content_sha256,state=None,
        rough_minutes=60,length_is_nics=True,said_by='owner',came_by='direct',speaker=None,words='make it an hour'),caller='damien')
    words='Record title: make it an hour'
    corrected=_door(provider,WorkRequest('pl-fix-door',words,'owner','direct',
        ({'move':'organise','thing':guessed.record_id,'rough_minutes':60,'length_is_nics':True,'words':words},)))
    current=corrected['things'][0]['record_id']
    assert corrected['status']=='accepted' and current!=guessed.record_id
    assert _nics_length(current,provider)==(60,60,'nic')
    assert _nics_length(guessed.record_id,provider)==(30,45,'damien')
    assert _stored(provider,current).record.supersedes==(guessed.record_id,)


def test_a_length_marked_as_nics_needs_his_words():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),_memory_request('pl-words','item'))
    _denied_change(fake,provider,_memory_change_request('pl-words-agent',saved.record_id,saved.content_sha256,state=None,
        rough_minutes=60,length_is_nics=True,**_agent(words='Nic said about an hour')),caller='damien')
    agent=_door(provider,WorkRequest('pl-words-agent-door','Nic said about an hour','other','connected_source',
        ({'move':'organise','thing':saved.record_id,'rough_minutes':60,'length_is_nics':True},),speaker='damien'))
    unchanged=_stored(provider,agent['things'][0]['record_id']).record
    assert 'duration_minutes' not in unchanged.data.get('timing',{})
    assert any(flag.get('dropped') for thing in agent['things'] for flag in thing['flags'])
    words='Record title: make it an hour'
    owner=_door(provider,WorkRequest('pl-words-owner-door',words,'owner','direct',
        ({'move':'organise','thing':unchanged.id,'rough_minutes':60,'length_is_nics':True,'words':words},)))
    assert _nics_length(owner['things'][0]['record_id'],provider)==(60,60,'nic')


def test_a_length_marked_as_nics_needs_the_number():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),
            replace(_memory_request('pl-nonumber','item'),length_is_nics=True))
    assert refused.value.code=='brain.invalid_argument'


@pytest.mark.parametrize("words", ['make it an hour', '60 minutes', '1 hour', "it's an hour"])
def test_nics_length_words_may_say_it_any_plain_way(words):
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('pl-say','item'),words=words,rough_minutes=60,length_is_nics=True))
    assert _nics_length(saved.record_id,provider)==(60,60,'nic')


@pytest.mark.parametrize("words", ['make it half an hour', 'Call the bank', 'an hour and a half', 'call at 60 Ang Mo Kio',
                                   'due 30 Sep'])
def test_nics_length_words_must_say_that_length(words):
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),
            replace(_memory_request('pl-mismatch','item'),words=words,rough_minutes=60,length_is_nics=True))
    assert refused.value.code=='brain.record.needs_owner_words'



# --- Placement slice 5: the receipt names the length for the agent to say ---------------------------


def test_an_agents_guess_comes_back_as_words_to_say():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=_as('damien',provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('pl5-guess','item'),block='admin',rough_minutes=30))
    assert saved.length=={'minutes':30,'planned_minutes':45,'estimated_by':'damien','yours':False,
        'text':'My guess: 30 min, planned as 45 min with padding. Say a number to change it.',
        'next':"His answer goes to bc_work: organise this thing with rough_minutes, length_is_nics and his quoted message."}


def test_nics_own_length_comes_back_as_his():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('pl5-own','item'),words='that takes 90 minutes',block='admin',rough_minutes=90,length_is_nics=True))
    assert saved.length['text']=='1 h 30 min, as you said.' and saved.length['yours'] is True
    assert 'next' not in saved.length


def test_a_sorted_task_with_no_length_asks_for_one():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),replace(_memory_request('pl5-none','item'),block='admin'))
    assert saved.length=={'status':'needs_length','text':'Saved. How long will it take?','next':"His answer goes to bc_work: organise this thing with rough_minutes, length_is_nics and his quoted message."}


def test_a_change_names_the_length_only_when_it_carries_one():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=_as('damien',provider.save,_authorization(Op.MEMORY_SAVE,'write'),
        replace(_memory_request('pl5-chg','item'),title='Record title',block='admin',rough_minutes=30))
    _denied_change(fake,provider,_memory_change_request('pl5-chg-wait',saved.record_id,saved.content_sha256),caller='damien')
    waited=_door(provider,WorkRequest('pl5-wait-door','Record title needs Sam','owner','direct',
        ({'move':'wait','thing':saved.record_id,'kind':'needs','who':'Sam'},)))
    assert 'length' not in waited
    current=waited['things'][0]['record_id']
    assert _nics_length(current,provider)==(30,45,'damien')
    _denied_change(fake,provider,_memory_change_request('pl5-chg-fix',current,
        hashlib.sha256(_stored(provider,current).content).hexdigest(),state=None,rough_minutes=60,length_is_nics=True,
        said_by='owner',came_by='direct',speaker=None,words='make it an hour'),caller='damien')
    words='Record title: make it an hour'
    fixed=_door(provider,WorkRequest('pl5-fix-door',words,'owner','direct',
        ({'move':'organise','thing':current,'rough_minutes':60,'length_is_nics':True,'words':words},)))
    assert _nics_length(fixed['things'][0]['record_id'],provider)==(60,60,'nic')
    assert 'Record title' in fixed['say'] and fixed['things'][0]['id']==saved.record_id


def test_items_with_no_block_and_no_length_keep_the_old_receipt():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    saved=anyio.run(provider.save,_authorization(Op.MEMORY_SAVE,'write'),_memory_request('pl5-plain','item'))
    assert not hasattr(saved,'length')



def test_a_projects_steps_say_their_lengths():
    fake,storage,clock=setup()
    provider=GitHubBrainProvider(storage,_adapter(operation_time=clock))
    receipt=_door(provider,_plan())
    actual=[item.record for item in provider._storage.snapshot_records(kinds=frozenset({'item'})).records]
    goal=next(record for record in actual if 'parent' not in record.data)
    boxes=next(record for record in actual if record.title=='Buy boxes')
    assert boxes.data['parent']==goal.id
    assert boxes.data['timing']=={'duration_minutes':45,'padded_minutes':68,'estimated_by':'hermes-wa'}
    assert 'duration_minutes' not in goal.data.get('timing',{})
    assert {thing['record_id'] for thing in receipt['things']} <= {record.id for record in actual}
    assert '0 of 3' in receipt['say'] and 'and 1 more' in receipt['say']
    words='Buy boxes takes 45 minutes, as I said'
    correction=_door(provider,WorkRequest('c7-own-length',words,'owner','direct',
        ({'move':'organise','thing':boxes.id,'rough_minutes':45,'length_is_nics':True,'words':words},)))
    [thing]=correction['things']
    corrected=_stored(provider,thing['record_id']).record
    assert corrected.data['parent']==goal.id and corrected.supersedes==(boxes.id,)
    assert corrected.data['timing']=={'duration_minutes':45,'padded_minutes':45,'estimated_by':'nic'}
    assert boxes.data['timing']['padded_minutes']==68


def _hub_storage(fake):
    """A storage as the hub builds it, with an object cache: Brain Core refuses the hub's 5,000-file scan
    cap without one (docs/product/2026-09-30-brain-home-storage-limit-plan.md, layer 1)."""

    from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache

    from test_github_brain_provider import _HistoryTransport

    return GitHubBrainHomeStorage(
        repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN, transport=_HistoryTransport(fake),
        object_cache=GitHubObjectCache(),
    )
