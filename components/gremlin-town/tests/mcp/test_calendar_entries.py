"""Calendar entries added automatically when a task has a start time (N8; step E spec §5; rebuild
plan slice 6, gaps G7 and G8; ROA-66).

Drives a real `GitHubBrainProvider` (Brain Core's fake GitHub transport, real record validation
and time words) with a real `CalendarWriter` over a small in-memory Google Calendar behind
`httpx.MockTransport`, so every Google request is recorded exactly. Nothing reaches Google.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import anyio
import httpx
import pytest

from test_github_brain_provider import (
    FIRST_TIME, LANE_B, _adapter, _authorization, _memory_request, _memory_change_request, _HistoryTransport,
)
from brain_core.records import new_record_id, record_from_mapping, render_record
from gremlin_town.api.serialization import to_jsonable
from gremlin_town.mcp.brain_runtime import (
    BrainOperation as Op,
    BrainRuntimeError,
    CalendarRemoveRequest,
    MemoryListQuery,
    WorkRequest,
    WriteReceipt,
    WriteReceiptWithCalendar,
)
from gremlin_town.mcp.github_brain_provider import GitHubBrainProvider
from gremlin_town.mcp.host_identity import bind_write_attribution
from gremlin_town.review.calendar_entries import slot_for, slot_words
from gremlin_town.review.why_log import WhyLog, WhyLogError
from gremlin_town.sources.google.calendar_writer import CalendarWriter, EventSlot
from gremlin_town.sources.google.settings import GoogleSettings
from tests.sources.fake_google_calendar import EVENTS_PATH, FakeGoogleCalendar, FixedToken

SGT = timezone(timedelta(hours=8))


def test_dates_appointment_has_a_timed_slot_with_duration():
    from gremlin_town.review.calendar_entries import has_start_time
    record = SimpleNamespace(kind="item", state="open", data={"level": "accepted",
        "dates": {"appointment": {"day": "2026-10-02", "at": "13:00"}},
        "timing": {"duration_minutes": 45}})
    assert has_start_time(record)
    slot = slot_for(record)
    assert slot.start == datetime(2026, 10, 2, 13, 0, tzinfo=SGT)
    assert slot.end - slot.start == timedelta(minutes=45)


@pytest.mark.parametrize("kind", ["deadline", "planned", "not_before", "suggested"])
def test_nonappointment_work_dates_never_create_or_move_an_entry_even_with_old_timing(kind):
    from gremlin_town.review.calendar_entries import follow, has_start_time
    record = SimpleNamespace(kind="item", state="open", id="brn_fixture", supersedes=(), source=None,
        data={"level": "accepted", "dates": {kind: {"day": "2026-10-02", "at": "13:00"}},
              "timing": {"due": "2026-10-02", "at": "13:00"}})
    class UnusedWriter:
        def find(self, key):
            raise AssertionError("nonappointment must not reach Google")
    assert not has_start_time(record) and slot_for(record) is None
    assert follow(UnusedWriter(), record, changed=False) is None
    assert follow(UnusedWriter(), record, changed=True) is None


@pytest.mark.parametrize("level,said_by", [("suggested", "owner"), ("accepted", "other")])
def test_unaccepted_or_forwarded_appointment_never_reaches_google(level, said_by):
    from gremlin_town.review.calendar_entries import follow, has_start_time
    record = SimpleNamespace(kind="item", state="open", id="brn_fixture", supersedes=(),
        source={"said_by": said_by}, data={"level": level,
            "dates": {"appointment": {"day": "2026-10-02", "at": "13:00"}}})
    class UnusedWriter:
        def find(self, key):
            raise AssertionError("unaccepted or forwarded appointment must not reach Google")
    assert not has_start_time(record)
    assert follow(UnusedWriter(), record, changed=False) is None
    assert follow(UnusedWriter(), record, changed=True) is None


@pytest.mark.parametrize("attendees", [[], [{"email": "nic@example.test", "self": True}]])
def test_empty_invitees_or_calendar_owner_are_not_guests_and_no_invites_are_added(attendees):
    provider, google = _setup()
    saved = _save(provider, _dentist())
    google.events["evt1"]["attendees"] = attendees
    moved = _change(provider, "move-empty-invitees", saved, words="Dentist Friday 8pm", time_words="Friday 8pm")
    assert moved.calendar["status"] == "moved"
    patch = google.calls[-1]
    assert dict(httpx.QueryParams(patch.url.query)) == {"sendUpdates": "none"}
    assert "attendees" not in json.loads(patch.content)
    dropped = _drop(provider, "drop-empty-invitees", moved)
    assert dropped.calendar["status"] == "ask_remove"
    assert "DELETE" not in [method for method, _ in google.requests()]
    assert _remove(provider, dropped).status == "removed"
    assert dict(httpx.QueryParams(google.calls[-1].url.query)) == {"sendUpdates": "none"}


def _google() -> GoogleSettings:
    return GoogleSettings.from_table({"enabled": True, "account": "nic@example.test", "credential_env": "X"})


def _setup(*, review_enabled: bool = True, calendar_on: bool = True, why_log: WhyLog | None = None):
    fake_github = LANE_B.FakeGitHub()
    storage = _hub_storage(fake_github)
    moments: dict[str, datetime] = {}

    def clock(key):
        return moments.setdefault(key, FIRST_TIME + timedelta(seconds=len(moments)))

    google = FakeGoogleCalendar()
    writer = CalendarWriter(_google(), FixedToken(), transport=google.transport())
    provider = GitHubBrainProvider(
        storage, _adapter(operation_time=clock), review_enabled=review_enabled,
        calendar=writer if calendar_on else None,
        why_log=why_log if why_log is not None else WhyLog(":memory:"),
    )
    return provider, google


SAVE = _authorization(Op.MEMORY_SAVE, "write")
CHANGE = _authorization(Op.MEMORY_CHANGE, "write")
WORK = _authorization(Op.WORK, "write")


def _dentist(operation_id="dentist", *, time_words="Friday 7pm", title="Dentist", **overrides):
    words = f"{title} {time_words}"
    fields = dict(title=title, body=words, words=words, time_words=time_words)
    fields.update(overrides)
    return _memory_request(operation_id, "item", **fields)


def _save(provider, request):
    async def invoke():
        with bind_write_attribution('hermes-wa'):
            return await provider.save(SAVE, request)
    return anyio.run(invoke)


def _change(provider, operation_id, receipt, **overrides):
    """Translate only these fixture changes to public door moves, with trusted host attribution."""
    words = overrides['words']
    state = overrides.get('state')
    move = {'thing': receipt.record_id}
    if state == 'dropped':
        move.update(move='end', ending='dropped', reason=overrides['reason'], words=words)
    elif state == 'done':
        move.update(move='tick', what='thing', words=words)
    elif state == 'waiting':
        move.update(move='wait', kind='theirs', who='clinic')
    elif state == 'parked':
        move.update(move='park', until='5 October', words=words)
    elif state == 'open':
        # Undo the exact caller-owned drop; there is no state-edit route for items.
        move = {'move': 'undo', 'change': receipt.operation_id, 'words': words}
    elif 'time_words' in overrides:
        move.update(move='date', when=overrides['time_words'])
    elif 'rough_minutes' in overrides:
        move.update(move='organise', rough_minutes=overrides['rough_minutes'],
                    length_is_nics=overrides.get('length_is_nics', False), words=words)
    else:
        raise AssertionError(f'fixture change has no planned door translation: {overrides}')
    request = WorkRequest(operation_id, words, 'owner', 'direct', (move,))
    async def invoke():
        with bind_write_attribution('hermes-wa'):
            return await provider.work(WORK, request)
    reply = anyio.run(invoke)
    [thing] = reply['things']
    stored = next(item for item in provider._storage.snapshot_records(kinds=frozenset({'item'})).records
                  if item.record.id == thing['record_id'])
    # Compatibility is confined to the local test fixture. Provenance and content hash
    # come from the actual published record, not an invented receipt or alternate reader.
    return SimpleNamespace(operation_id=operation_id, status=reply['status'],
        record_id=thing['record_id'], version=reply['version'],
        content_sha256=hashlib.sha256(stored.content).hexdigest(),
        remote_observed=reply['remote_observed'], calendar=reply.get('calendar'),
        calendar_suggestion=None, record=stored.record)


def _drop(provider, operation_id, receipt):
    return _change(
        provider, operation_id, receipt, state="dropped", reason="Not going any more",
        words="Drop the dentist, not going any more",
    )


def _remove(provider, receipt, **overrides):
    fields = dict(operation_id="remove-dentist", record_id=receipt.record_id, said_by="owner",
                  came_by="direct", words="yes")
    fields.update(overrides)
    async def invoke():
        with bind_write_attribution('hermes-wa'):
            return await provider.remove_calendar_entry(CHANGE, CalendarRemoveRequest(**fields))
    return anyio.run(invoke)



# --- off: exactly as before ---------------------------------------------------

@pytest.mark.parametrize(("review_enabled", "calendar_on"), [(True, False), (False, True), (False, False)])
def test_off_the_confirmation_is_exactly_as_before_and_google_is_never_called(review_enabled, calendar_on):
    provider, google = _setup(review_enabled=review_enabled, calendar_on=calendar_on)
    saved = _save(provider, _dentist())

    assert type(saved) is WriteReceipt
    assert "calendar" not in to_jsonable(saved)
    assert saved.calendar_suggestion is None  # suggested and tap-to-add entries are retired
    moved = _change(provider, "move-dentist", saved, words="Dentist Friday 8pm", time_words="Friday 8pm")
    dropped = _drop(provider, "drop-dentist", moved)
    assert moved.status == dropped.status == "accepted"
    assert moved.calendar is None and dropped.calendar is None
    assert google.calls == []


# --- adding -------------------------------------------------------------------

def test_saving_an_accepted_task_with_a_start_time_adds_one_entry_with_no_guests():
    provider, google = _setup()
    saved = _save(provider, _dentist())

    assert saved.status == "accepted" and saved.remote_observed is True
    assert isinstance(saved, WriteReceiptWithCalendar)
    assert saved.calendar_suggestion is None  # a real entry, not a tap-to-add link
    assert saved.calendar == {
        "status": "added",
        "text": "Added to your calendar, Fri 25 Sep 7 pm, 30 min, under your calendar limit",
        "start": "2026-09-25T19:00:00+08:00",
        "end": "2026-09-25T19:30:00+08:00",
        "all_day": False,
        "minutes": 30,
    }
    assert google.requests() == [("GET", "/"), ("POST", "/")]
    lookup, insert = google.calls
    assert dict(httpx.QueryParams(lookup.url.query)) == {
        "privateExtendedProperty": f"exosuit={saved.record_id}", "maxResults": "10",
    }
    assert dict(httpx.QueryParams(insert.url.query)) == {"sendUpdates": "none"}
    assert json.loads(insert.content) == {
        "summary": "Dentist",
        "description": f"exosuit:{saved.record_id}",
        "start": {"dateTime": "2026-09-25T19:00:00+08:00", "timeZone": "Asia/Singapore"},
        "end": {"dateTime": "2026-09-25T19:30:00+08:00", "timeZone": "Asia/Singapore"},
        "extendedProperties": {"private": {"exosuit": saved.record_id}},
    }
    assert "attendees" not in json.loads(insert.content)


def test_a_replayed_save_finds_its_entry_by_the_marker_and_adds_nothing_twice():
    provider, google = _setup()
    first = _save(provider, _dentist())
    replay = _save(provider, _dentist())

    assert replay.status == "duplicate" and replay.record_id == first.record_id
    assert replay.calendar == first.calendar
    assert google.requests() == [("GET", "/"), ("POST", "/")]
    assert len(google.live()) == 1


def test_suggestions_and_ideas_never_get_an_entry():
    provider, google = _setup()
    suggested = _save(provider, _dentist("suggested-dentist", level="suggested", said_by="agent",
                                         came_by="agent_guess", speaker="fixture-agent"))
    idea = _save(provider, _dentist("idea-dentist", level="possible"))

    for receipt in (suggested, idea):
        assert type(receipt) is WriteReceipt
        assert receipt.calendar_suggestion is None  # no suggested entry or tap-to-add link
    assert google.calls == []


def test_a_task_with_only_a_date_has_no_entry_or_link_and_never_calls_google():
    provider, google = _setup()
    saved = _save(provider, _dentist(time_words="Friday"))

    assert type(saved) is WriteReceipt
    assert saved.calendar_suggestion is None  # a planned day is not an appointment entry
    assert google.calls == []


def test_a_task_given_a_start_time_later_gets_its_entry_then_marked_with_its_first_record():
    provider, google = _setup()
    saved = _save(provider, _dentist(time_words="Friday"))
    timed = _change(provider, "time-dentist", saved, words="Dentist Friday 7pm", time_words="Friday 7pm")

    assert timed.record_id != saved.record_id
    assert timed.calendar["status"] == "added"
    assert timed.calendar_suggestion is None
    [event] = google.live()
    assert event["description"] == f"exosuit:{saved.record_id}"
    assert event["extendedProperties"] == {"private": {"exosuit": saved.record_id}}


# --- moving -------------------------------------------------------------------

def test_changing_the_time_moves_the_entry_found_by_the_tasks_marker():
    provider, google = _setup()
    saved = _save(provider, _dentist())
    moved = _change(provider, "move-dentist", saved, words="Dentist Friday 8pm", time_words="Friday 8pm")

    assert moved.status == "accepted" and moved.record_id != saved.record_id
    assert moved.calendar == {
        "status": "moved",
        "text": "Moved on your calendar to Fri 25 Sep 8 pm, 30 min, under your calendar limit",
        "start": "2026-09-25T20:00:00+08:00",
        "end": "2026-09-25T20:30:00+08:00",
        "all_day": False,
        "minutes": 30,
    }
    assert google.requests() == [("GET", "/"), ("POST", "/"), ("GET", "/"), ("PATCH", "/evt1")]
    lookup, patch = google.calls[2:]
    # The marker is the task's first record ID, so the changed task still finds its entry.
    assert dict(httpx.QueryParams(lookup.url.query))["privateExtendedProperty"] == f"exosuit={saved.record_id}"
    assert dict(httpx.QueryParams(patch.url.query)) == {"sendUpdates": "none"}
    assert json.loads(patch.content) == {
        "start": {"dateTime": "2026-09-25T20:00:00+08:00", "timeZone": "Asia/Singapore"},
        "end": {"dateTime": "2026-09-25T20:30:00+08:00", "timeZone": "Asia/Singapore"},
    }
    [event] = google.live()
    assert event["summary"] == "Dentist" and event["description"] == f"exosuit:{saved.record_id}"


def test_a_change_that_keeps_the_time_leaves_the_entry_where_it_is():
    provider, google = _setup()
    saved = _save(provider, _dentist())
    waiting = _change(provider, "wait-dentist", saved, said_by="owner", came_by="direct",
                      words="Waiting for the clinic to confirm", state="waiting")

    assert waiting.calendar["status"] == "on_calendar"
    assert google.requests() == [("GET", "/"), ("POST", "/"), ("GET", "/")]


def test_door_replay_keeps_the_moved_calendar_receipt_without_another_brain_commit():
    provider, google = _setup()
    saved = _save(provider, _dentist())
    moved = _change(provider, "calendar-door-replay", saved,
        words="Dentist Friday 8pm", time_words="Friday 8pm")
    head = provider._storage.revision().commit_sha
    event = json.loads(json.dumps(google.live()[0]))
    before = len(google.calls)
    replay = _change(provider, "calendar-door-replay", saved,
        words="Dentist Friday 8pm", time_words="Friday 8pm")
    assert moved.calendar["status"] == "moved"
    assert replay.status == "duplicate" and replay.calendar == moved.calendar
    assert provider._storage.revision().commit_sha == head
    assert google.live() == [event]
    assert google.requests()[before:] == []  # exact successful acknowledgment is durable


def test_door_replay_retries_a_failed_calendar_move_once_without_another_brain_commit():
    provider, google = _setup()
    saved = _save(provider, _dentist())
    google.fail_next["PATCH"] = 500
    moved = _change(provider, "calendar-door-failed-replay", saved,
        words="Dentist Friday 8pm", time_words="Friday 8pm")
    assert moved.status == "accepted" and moved.calendar == {
        "status": "failed", "text": "Saved, but moving its calendar entry failed (provider_error)",
        "reason": "provider_error",
    }
    head = provider._storage.revision().commit_sha
    before = len(google.calls)
    replay = _change(provider, "calendar-door-failed-replay", saved,
        words="Dentist Friday 8pm", time_words="Friday 8pm")
    assert replay.status == "duplicate" and replay.calendar["status"] == "moved"
    assert provider._storage.revision().commit_sha == head
    assert google.requests()[before:] == [("GET", "/"), ("PATCH", "/evt1")]
    event = json.loads(json.dumps(google.live()[0]))
    before = len(google.calls)
    repeated = _change(provider, "calendar-door-failed-replay", saved,
        words="Dentist Friday 8pm", time_words="Friday 8pm")
    assert repeated.status == "duplicate" and repeated.calendar == replay.calendar
    assert google.live() == [event] and provider._storage.revision().commit_sha == head
    assert google.requests()[before:] == []
    assert google.requests() == [("GET", "/"), ("POST", "/"), ("GET", "/"), ("PATCH", "/evt1"),
        ("GET", "/"), ("PATCH", "/evt1")]


def _restart_calendar_provider(provider, path):
    provider._why_log.close()
    def no_materialization(_):
        raise AssertionError("exact calendar replay must not materialize another accepted time")
    return GitHubBrainProvider(provider._storage, _adapter(operation_time=no_materialization),
        review_enabled=True, calendar=provider._calendar, why_log=WhyLog(path))


def test_successful_calendar_acknowledgment_survives_a_provider_and_why_log_restart(tmp_path):
    path=tmp_path/'why.sqlite3'
    provider,google=_setup(why_log=WhyLog(path))
    saved=_save(provider,_dentist())
    moved=_change(provider,'calendar-restart',saved,words='Dentist Friday 8pm',time_words='Friday 8pm')
    head=provider._storage.revision().commit_sha
    before=list(google.calls)
    fresh=_restart_calendar_provider(provider,path)
    replay=_change(fresh,'calendar-restart',saved,words='Dentist Friday 8pm',time_words='Friday 8pm')
    assert replay.status=='duplicate' and replay.calendar==moved.calendar
    assert replay.version==moved.version and replay.content_sha256==moved.content_sha256
    assert fresh._storage.revision().commit_sha==head and google.calls==before
    fresh._why_log.close()


def test_calendar_effects_require_explicit_why_log_but_disabled_calendar_needs_no_state():
    fake=LANE_B.FakeGitHub()
    google=FakeGoogleCalendar()
    writer=CalendarWriter(_google(),FixedToken(),transport=google.transport())
    storage=_hub_storage(fake)
    with pytest.raises(BrainRuntimeError) as unavailable:
        GitHubBrainProvider(storage,_adapter(operation_time=lambda _:FIRST_TIME),review_enabled=True,calendar=writer)
    assert unavailable.value.code=='brain.capability.unavailable' and unavailable.value.field=='calendar'
    assert unavailable.value.fix=='supply the configured review why-log'
    assert google.calls==[] and fake.patch_payloads==[]
    provider=GitHubBrainProvider(storage,_adapter(operation_time=lambda _:FIRST_TIME),review_enabled=False,calendar=writer)
    saved=_save(provider,_dentist())
    assert saved.status=='accepted' and getattr(saved,'calendar',None) is None
    assert google.calls==[] and provider._why_log is None


def test_a_lost_calendar_ack_is_reconciled_without_claiming_the_unpublished_original_action(tmp_path,monkeypatch):
    provider,google=_setup(why_log=WhyLog(tmp_path/'why.sqlite3'))
    saved=_save(provider,_dentist())
    before_ids={item.record.id for item in provider._storage.snapshot_records(kinds=frozenset({'item'})).records}
    real=provider._why_log.record_calendar_effect
    def unavailable(**kwargs):
        raise WhyLogError('fixture crash before acknowledgment persisted')
    monkeypatch.setattr(provider._why_log,'record_calendar_effect',unavailable)
    with pytest.raises(WhyLogError,match='before acknowledgment'):
        _change(provider,'calendar-lost-ack',saved,words='Dentist Friday 8pm',time_words='Friday 8pm')
    head=provider._storage.revision().commit_sha
    after_ids={item.record.id for item in provider._storage.snapshot_records(kinds=frozenset({'item'})).records}
    [accepted_id]=after_ids-before_ids
    monkeypatch.setattr(provider._why_log,'record_calendar_effect',real)
    before=len(google.calls)
    replay=_change(provider,'calendar-lost-ack',saved,words='Dentist Friday 8pm',time_words='Friday 8pm')
    assert replay.status=='duplicate' and replay.record_id==accepted_id
    assert replay.calendar['status']=='on_calendar'  # first successfully observed acknowledgment
    assert google.requests()[before:]==[('GET','/')]
    assert provider._storage.revision().commit_sha==head and len(google.live())==1
    before=list(google.calls)
    repeated=_change(provider,'calendar-lost-ack',saved,words='Dentist Friday 8pm',time_words='Friday 8pm')
    assert repeated.calendar==replay.calendar and google.calls==before
    provider._why_log.close()


def test_guest_blocked_calendar_effect_can_retry_but_a_successful_ack_is_not_reapplied(tmp_path):
    provider,google=_setup(why_log=WhyLog(tmp_path/'why.sqlite3'))
    saved=_save(provider,_dentist())
    google.events['evt1']['attendees']=[{'email':'guest@example.test'}]
    moved=_change(provider,'calendar-guests-retry',saved,words='Dentist Friday 8pm',time_words='Friday 8pm')
    assert moved.calendar['status']=='has_guests'
    assert [method for method,_ in google.requests()]==['GET','POST','GET']
    head=provider._storage.revision().commit_sha
    del google.events['evt1']['attendees']  # local fake models Nic removing the guest
    before=len(google.calls)
    replay=_change(provider,'calendar-guests-retry',saved,words='Dentist Friday 8pm',time_words='Friday 8pm')
    assert replay.status=='duplicate' and replay.calendar['status']=='moved'
    assert google.requests()[before:]==[('GET','/'),('PATCH','/evt1')]
    before=list(google.calls)
    repeated=_change(provider,'calendar-guests-retry',saved,words='Dentist Friday 8pm',time_words='Friday 8pm')
    assert repeated.calendar==replay.calendar and google.calls==before
    assert provider._storage.revision().commit_sha==head
    provider._why_log.close()


def test_a_successful_calendar_slot_does_not_finalize_a_failed_slot_in_the_same_batch(tmp_path):
    provider,google=_setup(why_log=WhyLog(tmp_path/'why.sqlite3'))
    change=WorkRequest('calendar-two-slots','Dentist Friday 7pm and Physio Saturday 9am','owner','direct',
        ({'move':'new','title':'Dentist','when':'Friday 7pm'},
         {'move':'new','title':'Physio','when':'Saturday 9am'}))
    async def invoke():
        with bind_write_attribution('hermes-wa'):
            return await provider.work(WORK,change)
    google.fail_next['POST']=500
    first=anyio.run(invoke)
    assert first['status']=='accepted' and first['calendar']['status']=='failed'
    assert len(google.live())==1
    head=provider._storage.revision().commit_sha
    before=len(google.calls)
    replay=anyio.run(invoke)
    assert replay['status']=='duplicate' and replay['calendar']['status']=='added'
    assert google.requests()[before:]==[('GET','/'),('POST','/')]
    assert len(google.live())==2 and provider._storage.revision().commit_sha==head
    before=list(google.calls)
    repeated=anyio.run(invoke)
    assert repeated==replay and google.calls==before
    provider._why_log.close()


@pytest.mark.parametrize('archive',[False,True])
@pytest.mark.parametrize('first_failed',[False,True])
def test_historical_calendar_replay_never_rewinds_a_successor_or_resurrects_archive(tmp_path,archive,first_failed):
    path=tmp_path/'why.sqlite3'
    provider,google=_setup(why_log=WhyLog(path))
    saved=_save(provider,_dentist())
    if first_failed:
        google.fail_next['PATCH']=500
    moved=_change(provider,'calendar-historical',saved,words='Dentist Friday 8pm',time_words='Friday 8pm')
    assert moved.calendar['status']==('failed' if first_failed else 'moved')
    if archive:
        snapshot=provider._storage.snapshot_records(kinds=frozenset({'item'}))
        provider._storage.archive_records(paths=[item.path for item in snapshot.records],kinds=frozenset({'item'}))
    else:
        later=_change(provider,'calendar-later',moved,words='Dentist Friday 9pm',time_words='Friday 9pm')
        assert later.calendar['status']=='moved'
    head=provider._storage.revision().commit_sha
    before=list(google.calls)
    event=json.loads(json.dumps(google.live()))
    fresh=_restart_calendar_provider(provider,path)
    replay=_change(fresh,'calendar-historical',saved,words='Dentist Friday 8pm',time_words='Friday 8pm') if not archive else None
    if archive:
        # The fixture receipt helper deliberately reads live records. Invoke the
        # public door directly for a whole-archive replay, never a fallback reader.
        change=WorkRequest('calendar-historical','Dentist Friday 8pm','owner','direct',
            ({'move':'date','thing':saved.record_id,'when':'Friday 8pm'},))
        async def invoke():
            with bind_write_attribution('hermes-wa'):
                return await fresh.work(WORK,change)
        reply=anyio.run(invoke)
        assert reply['status']=='duplicate' and reply['version']==moved.version
        calendar=reply['calendar']
        with pytest.raises(BrainRuntimeError) as missing:
            anyio.run(fresh.read,_authorization(Op.MEMORY_READ,'read'),moved.record_id)
        assert missing.value.code=='brain.not_found'
    else:
        assert replay.status=='duplicate' and replay.version==moved.version
        calendar=replay.calendar
    assert calendar==({'status':'skipped','text':'Calendar left as it is: a later or archived version owns this effect.'}
        if first_failed else moved.calendar)
    assert google.calls==before and google.live()==event
    assert fresh._storage.revision().commit_sha==head
    fresh._why_log.close()


def test_calendar_journal_is_not_consulted_by_invalid_replay_check_only_or_marker(tmp_path,monkeypatch):
    provider,google=_setup(why_log=WhyLog(tmp_path/'why.sqlite3'))
    saved=_save(provider,_dentist())
    moved=_change(provider,'calendar-journal-guards',saved,words='Dentist Friday 8pm',time_words='Friday 8pm')
    head=provider._storage.revision().commit_sha
    before=list(google.calls)
    def forbidden(**kwargs):
        raise AssertionError('an unverified or check-only call must not read/write calendar receipt state')
    monkeypatch.setattr(provider._why_log,'calendar_effect',forbidden)
    monkeypatch.setattr(provider._why_log,'record_calendar_effect',forbidden)
    original=WorkRequest('calendar-journal-guards','Dentist Friday 8pm','owner','direct',
        ({'move':'date','thing':saved.record_id,'when':'Friday 8pm'},))
    from dataclasses import replace
    async def invoke(change,authorization=WORK,caller='hermes-wa'):
        with bind_write_attribution(caller):
            return await provider.work(authorization,change)
    with pytest.raises(BrainRuntimeError) as changed:
        anyio.run(invoke,replace(original,message='Dentist Friday 9pm'))
    assert changed.value.code=='brain.conflict' and changed.value.fix
    with pytest.raises(BrainRuntimeError) as owner:
        anyio.run(invoke,original,replace(WORK,owner_id='another-owner'))
    assert owner.value.code=='ACCESS_DENIED' and owner.value.field=='scope' and owner.value.fix
    with pytest.raises(BrainRuntimeError) as caller:
        anyio.run(invoke,original,WORK,'local-codex')
    assert caller.value.code=='ACCESS_DENIED' and caller.value.fix
    checked=anyio.run(invoke,replace(original,check_only=True))
    assert checked['status']=='validated' and 'calendar' not in checked
    provider.replay_marker=tmp_path/'work-replay-check-only'
    provider.replay_marker.write_text('fixture marker')
    checked=anyio.run(invoke,original)
    assert checked['status']=='validated' and 'calendar' not in checked
    assert google.calls==before and provider._storage.revision().commit_sha==head
    provider._why_log.close()


def test_a_task_that_loses_its_time_leaves_its_existing_entry_untouched():
    from gremlin_town.review.calendar_entries import follow

    # Item memory.change has retired and the door has no clear-time move. Literal
    # legacy versions retain this backward-reader/reconciliation check until W10
    # removes all-day entries; fixture seeding is not an alternate production writer.
    fake = LANE_B.FakeGitHub()
    google = FakeGoogleCalendar()
    writer = CalendarWriter(_google(), FixedToken(), transport=google.transport())
    provider = GitHubBrainProvider(_hub_storage(fake), _adapter(operation_time=lambda key: FIRST_TIME),
        review_enabled=True, calendar=writer, why_log=WhyLog(":memory:"))
    words = "Dentist Friday 7pm"
    initial = record_from_mapping({
        "schema": "brain.record/v2", "id": new_record_id(FIRST_TIME, "a" * 16), "kind": "item",
        "title": "Dentist", "state": "open", "created_at": FIRST_TIME.isoformat(),
        "updated_at": FIRST_TIME.isoformat(), "sensitivity": "private",
        "vaults": [SAVE.owner_locator], "owners": [SAVE.owner_id], "entities": [],
        "evidence": [{"source_type": "session_message", "locator": "fixture://calendar/initial",
            "content_sha256": hashlib.sha256(words.encode()).hexdigest(), "excerpt": words, "actor": "hermes-wa"}],
        "supersedes": [], "dedupe_key": None, "body": words,
        "source": {"said_by": "owner", "came_by": "direct", "recorded_by": "hermes-wa", "words": words},
        "data": {"level": "accepted", "size": "task", "timing": {
            "due": "2026-09-25", "at": "19:00",
            "resolved_from": {"words": "Friday 7pm", "today": "2026-09-21"}}}, "links": [],
    })
    first_bytes = render_record(initial).encode()
    first_path = f"records/item/2026/09/{initial.id}.md"
    fake.seed({first_path: first_bytes})
    provider._records.remember_record(SAVE, initial.id, first_path)
    read = anyio.run(provider.read, _authorization(Op.MEMORY_READ, "read"), initial.id)
    assert read.canonical_record == initial.as_dict()
    assert follow(writer, record_from_mapping(read.canonical_record), changed=False)["status"] == "added"
    saved = SimpleNamespace(record_id=initial.id)
    head = fake.branch
    async def retired_change():
        with bind_write_attribution("hermes-wa"):
            return await provider.change(CHANGE, _memory_change_request("date-dentist", initial.id,
                hashlib.sha256(first_bytes).hexdigest(), words="Dentist Saturday", time_words="Saturday",
                said_by="owner", came_by="direct", speaker=None))
    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(retired_change)
    assert refused.value.code == "ACCESS_DENIED" and refused.value.fix == "use bc_work"
    assert fake.branch == head and google.requests() == [("GET", "/"), ("POST", "/")]

    later = FIRST_TIME + timedelta(seconds=1)
    successor = initial.as_dict()
    successor.update(id=new_record_id(later, "b" * 16), created_at=later.isoformat(),
        updated_at=later.isoformat(), supersedes=[initial.id])
    successor["data"] = {"level": "accepted", "size": "task", "timing": {
        "due": "2026-09-26", "resolved_from": {"words": "Saturday", "today": "2026-09-21"}}}
    successor["source"] = {**successor["source"], "words": "Dentist Saturday"}
    successor["evidence"].append({"source_type": "session_message", "locator": "fixture://calendar/successor",
        "content_sha256": hashlib.sha256(b"Dentist Saturday").hexdigest(), "excerpt": "Dentist Saturday",
        "actor": "hermes-wa"})
    successor = record_from_mapping(successor)
    successor_path = f"records/item/2026/09/{successor.id}.md"
    fake.seed({successor_path: render_record(successor).encode()})
    provider._records.remember_record(SAVE, successor.id, successor_path)
    read = anyio.run(provider.read, _authorization(Op.MEMORY_READ, "read"), successor.id)
    assert read.canonical_record == successor.as_dict()
    dated = SimpleNamespace(calendar=follow(writer, record_from_mapping(read.canonical_record), changed=True),
        calendar_suggestion=None)

    assert dated.calendar is None  # no all-day replacement and no automatic removal
    assert dated.calendar_suggestion is None
    assert google.requests() == [("GET", "/"), ("POST", "/")]
    [event] = google.live()
    assert event["id"] == "evt1" and not event.get("attendees")
    assert event["description"] == f"exosuit:{saved.record_id}"
    assert event["extendedProperties"] == {"private": {"exosuit": saved.record_id}}


def test_a_parked_task_leaves_its_entry_alone_without_asking_google():
    provider, google = _setup()
    saved = _save(provider, _dentist())
    parked = _change(provider, "park-dentist", saved, state="parked", parked_until="2026-10-05",
                     words="Park the dentist till 5 October")

    assert parked.status == "accepted" and parked.calendar is None
    assert google.requests() == [("GET", "/"), ("POST", "/")]


# --- removing -----------------------------------------------------------------

def test_dropping_asks_before_removing_and_removes_nothing_itself():
    provider, google = _setup()
    saved = _save(provider, _dentist())
    dropped = _drop(provider, "drop-dentist", saved)

    assert dropped.status == "accepted"
    assert dropped.calendar == {
        "status": "ask_remove",
        "text": "It's on your calendar, Fri 25 Sep 7 pm, 30 min. Remove it from your calendar?",
        "question": "Remove it from your calendar?",
        "start": "2026-09-25T19:00:00+08:00",
        "end": "2026-09-25T19:30:00+08:00",
        "all_day": False,
        "minutes": 30,
    }
    assert "DELETE" not in [method for method, _ in google.requests()]
    assert len(google.live()) == 1


def test_done_asks_too():
    provider, google = _setup()
    saved = _save(provider, _dentist())
    done = _change(provider, "done-dentist", saved, said_by="owner", came_by="direct",
                   words="The dentist visit is done", state="done")

    assert done.calendar["status"] == "ask_remove"
    assert len(google.live()) == 1


@pytest.mark.parametrize("caller", ["chatgpt-web", "claude-web", "fleet-boss-grok", "local-codex", "system:unknown"])
def test_direct_provider_calendar_removal_repeats_door_gate_before_effects(caller):
    provider, google = _setup()
    saved = _save(provider, _dentist())
    dropped = _drop(provider, "drop-gate-dentist", saved)
    before = list(google.calls)
    head = provider._storage.revision().commit_sha
    async def invoke():
        with bind_write_attribution(caller):
            return await provider.remove_calendar_entry(CHANGE,
                CalendarRemoveRequest("remove-gate-dentist", dropped.record_id, "owner", "direct", "yes"))
    with pytest.raises(BrainRuntimeError) as refused:
        anyio.run(invoke)
    assert refused.value.code == "ACCESS_DENIED" and refused.value.field == "caller"
    assert refused.value.fix == "remove it through Diablo"
    assert google.calls == before and len(google.live()) == 1
    assert provider._storage.revision().commit_sha == head


def test_only_a_separate_call_with_nics_own_yes_removes_the_marked_entry():
    provider, google = _setup()
    other = _save(provider, _dentist("other-task", title="Physio", time_words="Thursday 9am"))
    saved = _save(provider, _dentist())
    dropped = _drop(provider, "drop-dentist", saved)
    before = list(google.calls)

    for refused in (dict(said_by="agent"), dict(came_by="agent_guess"), dict(words="   "), dict(words=None)):
        with pytest.raises(BrainRuntimeError) as caught:
            _remove(provider, dropped, **refused)
        assert caught.value.code == "brain.record.needs_owner_words"
        assert caught.value.message == (
            "Removing a calendar entry needs Nic's own yes: said_by owner, came_by direct, and his exact words")
        assert caught.value.fix == "quote Nic's message and set said_by owner, came_by direct"
    assert google.calls == before  # a refused removal never reaches Google

    removed = _remove(provider, dropped)
    assert (removed.status, removed.text, removed.record_id) == ("removed", "Removed from your calendar", dropped.record_id)
    lookup, delete = google.calls[len(before):]
    assert dict(httpx.QueryParams(lookup.url.query))["privateExtendedProperty"] == f"exosuit={saved.record_id}"
    assert (delete.method, delete.url.path) == ("DELETE", f"{EVENTS_PATH}/evt2")
    assert dict(httpx.QueryParams(delete.url.query)) == {"sendUpdates": "none"}
    # Only the dropped task's entry went; the other task's entry is untouched.
    assert [event["extendedProperties"]["private"]["exosuit"] for event in google.live()] == [other.record_id]

    again = _remove(provider, dropped, operation_id="remove-dentist-again")
    assert again.status == "not_found"
    assert google.requests()[-1] == ("GET", "/")


def test_removal_is_refused_for_a_task_still_open_or_changed_since():
    provider, google = _setup()
    saved = _save(provider, _dentist())
    with pytest.raises(BrainRuntimeError) as still_open:
        _remove(provider, saved)
    assert still_open.value.code == "brain.invalid_argument"

    dropped = _drop(provider, "drop-dentist", saved)
    _change(provider, "reopen-dentist", dropped, state="open", words="Actually I am going to the dentist")
    with pytest.raises(BrainRuntimeError) as reopened:
        _remove(provider, dropped)
    assert reopened.value.code == "brain.record.changed"
    assert len(google.live()) == 1
    assert "DELETE" not in [method for method, _ in google.requests()]


def test_removal_needs_calendar_entries_switched_on():
    provider, _ = _setup(calendar_on=False)
    saved = _save(provider, _dentist())
    with pytest.raises(BrainRuntimeError) as caught:
        _remove(provider, saved)
    assert caught.value.code == "brain.capability.unavailable"


# --- guests, failures ----------------------------------------------------------

def test_an_entry_someone_gave_guests_is_never_moved_asked_about_or_removed():
    provider, google = _setup()
    saved = _save(provider, _dentist())
    google.events["evt1"]["attendees"] = [{"email": "nic@example.test", "self": True}, {"email": "sam@example.test"}]

    moved = _change(provider, "move-dentist", saved, words="Dentist Friday 8pm", time_words="Friday 8pm")
    assert moved.calendar["status"] == "has_guests"
    assert moved.calendar["text"] == "Its calendar entry has guests now, so Exosuit left it as it is"
    dropped = _drop(provider, "drop-dentist", moved)
    assert dropped.calendar["status"] == "has_guests"
    assert _remove(provider, dropped).status == "has_guests"
    assert [method for method, _ in google.requests()] == ["GET", "POST", "GET", "GET", "GET"]


def test_a_failed_calendar_write_never_blocks_the_brain_save_and_a_replay_tries_again():
    provider, google = _setup()
    google.fail_next["POST"] = 500
    saved = _save(provider, _dentist())

    assert saved.status == "accepted" and saved.remote_observed is True
    assert saved.calendar == {
        "status": "failed",
        "text": "Saved, but adding it to your calendar failed (provider_error)",
        "reason": "provider_error",
    }
    listed = anyio.run(provider.list, _authorization(Op.MEMORY_LIST, "read"), MemoryListQuery())
    assert [item.record_id for item in listed.items] == [saved.record_id]
    assert google.live() == []

    replay = _save(provider, _dentist())
    assert replay.status == "duplicate" and replay.calendar["status"] == "added"
    assert len(google.live()) == 1


def test_a_calendar_that_cannot_be_reached_is_said_plainly():
    provider, google = _setup()
    google.fail_next["GET"] = 403
    saved = _save(provider, _dentist())
    assert saved.status == "accepted"
    assert saved.calendar["status"] == "failed"
    assert saved.calendar["text"] == "Saved, but checking your calendar failed (permission_denied)"


def test_an_unexpected_calendar_bug_still_never_blocks_the_save(monkeypatch):
    from gremlin_town.review import calendar_entries

    def broken(*_args, **_kwargs):
        raise KeyError("a bug, not a Google failure")

    monkeypatch.setattr(calendar_entries, "follow", broken)
    provider, _ = _setup()
    saved = _save(provider, _dentist())
    assert saved.status == "accepted" and saved.remote_observed is True
    assert saved.calendar["status"] == "failed"
    assert saved.calendar["reason"] == "internal_error"


# --- the slot and its words ------------------------------------------------------

def _record(**timing):
    return SimpleNamespace(data={"timing": timing})


@pytest.mark.parametrize(("timing", "words"), [
    ({"due": "2026-10-02", "at": "19:00"}, "Fri 2 Oct 7 pm, 30 min"),
    ({"due": "2026-10-02", "at": "07:30"}, "Fri 2 Oct 7:30 am, 30 min"),
    ({"due": "2026-10-02", "at": "12:00"}, "Fri 2 Oct 12 pm, 30 min"),
    ({"due": "2026-10-02", "at": "00:00"}, "Fri 2 Oct 12 am, 30 min"),
    ({"due": "2026-10-02", "at": "19:00", "duration_minutes": 90}, "Fri 2 Oct 7 pm, 90 min"),
    ({"due": "2026-10-02"}, None),
    ({"window": {"start": "2026-10-05", "end": "2026-10-11"}}, None),
])
def test_the_slot_and_its_plain_words(timing, words):
    slot = slot_for(_record(**timing))
    assert (slot_words(slot) if slot is not None else None) == words


def test_a_timed_slot_is_singapore_time_and_30_minutes_unless_the_task_has_a_length():
    assert slot_for(_record(due="2026-10-02", at="19:00")) == EventSlot(
        datetime(2026, 10, 2, 19, 0, tzinfo=SGT), datetime(2026, 10, 2, 19, 30, tzinfo=SGT),
    )
    assert slot_for(_record(due="2026-10-02", at="23:45", duration_minutes=45)).end == datetime(
        2026, 10, 3, 0, 30, tzinfo=SGT,
    )
    assert slot_for(_record(due="2026-10-02")) is None
    assert slot_for(_record()) is None


def _hub_storage(fake):
    """A storage as the hub builds it, with an object cache: Brain Core refuses the hub's 5,000-file scan
    cap without one (docs/product/2026-09-30-brain-home-storage-limit-plan.md, layer 1)."""

    from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache

    return GitHubBrainHomeStorage(
        repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN,
        transport=_HistoryTransport(fake),
        object_cache=GitHubObjectCache(),
    )
