"""Synthetic records and an HTTP fake checked against the official 2 Oct API."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
import pytest

from brain_core.records import Record
from gremlin_town.flowsavvy.client import FlowSavvyClient, FlowSavvyError
from gremlin_town.flowsavvy.settings import FlowSavvySettings
from gremlin_town.flowsavvy.snapshot import read_snapshot
from gremlin_town.flowsavvy.state import SyncState, notify_change
from gremlin_town.flowsavvy.sync import sync_once

NOW = datetime(2026, 10, 2, 10, tzinfo=timezone(timedelta(hours=8)))


def item(n=1, *, state="open", minutes=30, **data):
    return Record(schema="brain.record/v2", id=f"brn_20261002_000000_{n:016x}", kind="item",
                  title=f"Synthetic task {n}", state=state, created_at=NOW, updated_at=NOW,
                  assertion="explicit", confidence=None, sensitivity="private", vaults=(), owners=(),
                  entities=(), evidence=(), supersedes=(), dedupe_key=None,
                  data=dict(level="accepted", size="task", timing={} if minutes is None else
                            {"duration_minutes": minutes, "estimated_by": "nic"}, **data))


class FakeAPI:
    def __init__(self):
        self.contract = json.loads((Path(__file__).parent / "flowsavvy-openapi.json").read_text())
        self.items = {}
        self.calls = []
        self.schedule = []
        self.fail_create = False
        self.fail_schedule = False
        self.created_count = 0

    def __call__(self, request):
        path, method = request.url.path, request.method.lower()
        route = path
        if path.startswith("/api/items/"):
            route = "/api/items/{id}"
        if path.startswith("/api/tasks/"):
            route = "/api/tasks/{id}" + ("/" + path.split("/")[-1] if len(path.split("/")) == 5 else "")
        assert method in self.contract["paths"][route]
        self.calls.append((method, path, dict(request.url.params)))
        if request.content:
            body = json.loads(request.content)
            schemas = self.contract["components"]["schemas"]
            fields = schemas["Item"]["properties"] | schemas["Task"]["allOf"][1]["properties"]
            assert set(body) <= set(fields)
            assert body["itemType"] == "task"
            for name, value in body.items():
                field = fields[name]
                allowed = field.get("type", [])
                allowed = [allowed] if isinstance(allowed, str) else allowed
                if value is None:
                    assert "null" in allowed
                elif "enum" in field:
                    assert value in field["enum"]
                elif "integer" in allowed:
                    assert type(value) is int
                elif "boolean" in allowed:
                    assert type(value) is bool
                elif "array" in allowed:
                    assert isinstance(value, list)
                elif "string" in allowed:
                    assert isinstance(value, str)
            assert 0 < body["durationMinutes"] < 1440
        if path == "/api/tasks":
            self.created_count += 1
            remote = f"synthetic-{self.created_count}"
            self.items[remote] = dict(body, id=remote, completed=False, bufferBeforeMinutes=7)
            if self.fail_create:
                raise httpx.ReadTimeout("SECRET must not leak", request=request)
            return httpx.Response(200, json=self.items[remote])
        if path == "/api/schedule":
            if self.fail_schedule:
                return httpx.Response(503, text="PRIVATE remote error")
            return httpx.Response(200, json={"scheduleItems": self.schedule})
        if path == "/api/recalculate":
            return httpx.Response(204)
        remote = path.split("/")[3]
        if remote not in self.items:
            return httpx.Response(404)
        if path.endswith("/uncomplete"):
            self.items[remote]["completed"] = False
            return httpx.Response(204)
        if path.endswith("/complete"):
            self.items[remote]["completed"] = True
        elif method == "delete":
            del self.items[remote]
            return httpx.Response(204)
        elif method == "put":
            # The official endpoint REPLACES, so the connector must GET first.
            self.items[remote] = dict(body, id=remote, completed=self.items[remote]["completed"])
        return httpx.Response(200, json=self.items[remote])


@pytest.fixture
def setup(tmp_path):
    settings = FlowSavvySettings.from_table(dict(enabled=True, state_file=str(tmp_path / "state.db"),
        token_file=str(tmp_path / "credential"), list_id="synthetic-list", admin_hours_id="hours-admin",
        deep_focus_hours_id="hours-focus", errands_hours_id="hours-errands"), base=tmp_path)
    fake = FakeAPI()
    client = FlowSavvyClient("synthetic-test-key", transport=httpx.MockTransport(fake))
    state = SyncState(settings.state_file)
    yield settings, fake, client, state
    client.close()
    state.close()


def run(setup, records, **kwargs):
    settings, _, client, state = setup
    return sync_once(records=records, client=client, state=state, settings=settings, now=NOW, **kwargs)


def test_task_fields_padding_dependency_and_seven_day_snapshot(setup):
    settings, fake, _, _ = setup
    parent = item(1, called="project", dates={"deadline": {"day": "2026-10-02"}})
    first = item(2, parent=parent.id, order=1, block="deep_focus", minutes=120,
                 dates={"not_before": {"day": "2026-10-03"}})
    second = item(3, parent=parent.id, order=2, after=[first.id], block="admin")
    second = replace(second, data=dict(second.data, timing={"duration_minutes": 31, "estimated_by": "agent:test"}))
    before = deepcopy((parent, first, second))
    result = run(setup, before)
    assert result["created"] == 2
    assert (parent, first, second) == before
    one, two = fake.items.values()
    assert one["durationMinutes"] == 120 and one["minLengthMinutes"] == 60
    assert one["schedulingHoursId"] == "hours-focus"
    assert one["canBeStartedAt"] == "2026-10-03T00:00:00"
    assert one["dueDateTime"] == "2026-10-02T23:59:59"
    assert parent.title in one["title"]
    assert two["durationMinutes"] == two["minLengthMinutes"] == 47
    assert two["blockedByIds"] == [one["id"]]
    assert two["priority"] == "asap" and two["isAutoIgnored"] is False
    assert ("get", "/api/schedule", {"startDate": "2026-10-02", "endDate": "2026-10-08"}) in fake.calls
    assert read_snapshot(settings.state_file, NOW).available
    run(setup, before)
    assert sum(method == "post" and path == "/api/tasks" for method, path, _ in fake.calls) == 2


@pytest.mark.parametrize("change", ["parked", "dropped", "waiting", "unsized", "appointment", "routine"])
def test_removed_when_no_longer_eligible(setup, change):
    original = item()
    run(setup, [original])
    if change in {"parked", "dropped", "waiting"}:
        changed = replace(original, state=change)
    elif change == "unsized":
        changed = item(minutes=None)
    elif change == "appointment":
        changed = item(dates={"appointment": {"day": "2026-10-02", "time": "12:00"}})
    else:
        changed = replace(original, data={"level": "routine", "size": "task"})
    assert run(setup, [changed])["removed"] == 1
    assert not setup[1].items


def test_done_completes_reopened_uncompletes_and_preserves_remote_fields(setup):
    r = item()
    run(setup, [r])
    run(setup, [replace(r, state="done")])
    assert next(iter(setup[1].items.values()))["completed"] is True
    run(setup, [replace(r, title="Synthetic changed title")])
    remote = next(iter(setup[1].items.values()))
    assert remote["completed"] is False
    assert remote["bufferBeforeMinutes"] == 7


def test_ambiguous_create_is_held_durably_no_duplicate_or_private_error(setup):
    settings, fake, client, state = setup
    fake.fail_create = True
    with pytest.raises(FlowSavvyError) as error:
        run(setup, [item()])
    assert "SECRET" not in str(error.value)
    fake.fail_create = False
    with SyncState(settings.state_file) as restarted:
        with pytest.raises(FlowSavvyError, match="reconciliation"):
            sync_once(records=[item()], client=client, state=restarted, settings=settings, now=NOW)
    assert len(fake.items) == 1
    assert not read_snapshot(settings.state_file, NOW).available


def test_snapshot_age_errors_and_done_remote_do_not_write_back(setup):
    settings, fake, _, _ = setup
    r = item(dates={"deadline": {"day": "2026-10-02"}})
    run(setup, [r])
    remote = next(iter(fake.items))
    fake.schedule = [{"itemType": "task", "itemId": remote, "startTime": "2026-10-03T10:00:00",
        "endTime": "2026-10-03T10:30:00", "completed": False, "thisPartCompleted": False,
        "isAutoScheduled": True, "allDay": False, "title": "Synthetic task"}]
    run(setup, [r])
    snapshot = read_snapshot(settings.state_file, NOW + timedelta(seconds=120))
    assert snapshot.age_seconds == 120
    assert snapshot.blocks[0].item_ref == r.id
    assert snapshot.blocks[0].end > snapshot.blocks[0].deadline
    assert not read_snapshot(settings.state_file, NOW + timedelta(seconds=601)).available
    fake.fail_schedule = True
    with pytest.raises(FlowSavvyError):
        run(setup, [r])
    failed = read_snapshot(settings.state_file, NOW)
    assert failed.message == "Couldn't see the schedule" and failed.blocks == ()
    assert failed.fetched_at == NOW


def test_default_off_and_durable_wake(setup, tmp_path):
    assert not FlowSavvySettings.from_table(None, base=tmp_path).enabled
    settings, _, _, state = setup
    initial = state.wake_version()
    notify_change(settings)
    assert state.wake_version() == initial + 1
    notify_change(replace(settings, enabled=False))
    assert state.wake_version() == initial + 1


def test_all_step_order_preserved_including_unsized_or_waiting_predecessor(setup):
    parent = item(1)
    first = item(2, parent=parent.id, order=1)
    second = item(3, parent=parent.id, order=2)
    run(setup, [parent, first, second])
    one, two = setup[1].items.values()
    assert two["blockedByIds"] == [one["id"]]
    unsized = replace(first, data=dict(first.data, timing={}))
    run(setup, [parent, unsized, second])
    assert not setup[1].items  # Never schedule second as if first didn't exist.
    run(setup, [parent, replace(first, state="done"), second])
    assert len(setup[1].items) == 1
    run(setup, [parent, replace(first, state="done"), replace(second, state="done")])
    assert len(setup[1].items) == 1  # Completing steps must not schedule their parent.
    assert next(iter(setup[1].items.values()))["completed"]


def test_ancestor_wait_and_appointment_excluded(setup):
    parent = item(1, needs={"from": "Synthetic colleague", "what": "fixture reply"})
    child = item(2, parent=parent.id)
    assert run(setup, [parent, child])["created"] == 0
    parent = item(1, dates={"not_before": {"event": "Synthetic event"}})
    assert run(setup, [parent, child])["created"] == 0


@pytest.mark.parametrize("sensitivity", ["restricted", "local-only"])
@pytest.mark.parametrize("depth", [1, 2])
def test_ancestor_sensitivity_prevents_payload_and_removes_previously_sent_child(setup, sensitivity, depth):
    ancestor = replace(item(1), sensitivity=sensitivity, title="Synthetic protected parent")
    child = replace(item(2, parent=ancestor.id), sensitivity="public")
    records = [ancestor, child]
    if depth == 2:
        records.append(item(3, parent=child.id))
    assert run(setup, records)["created"] == 0
    assert not setup[1].items
    assert not any(method == "post" and path == "/api/tasks" for method, path, _ in setup[1].calls)
    permitted = [replace(ancestor, sensitivity="private"), *records[1:]]
    assert run(setup, permitted)["created"] == 1
    assert run(setup, records)["removed"] == 1
    assert not setup[1].items


@pytest.mark.parametrize("state", ["waiting", "blocked"])
def test_ancestor_unspecified_wait_suppresses_descendants_but_dated_wait_can_schedule(setup, state):
    ancestor = item(1, state=state)
    parent = item(2, parent=ancestor.id)
    child = item(3, parent=parent.id)
    assert run(setup, [ancestor, parent, child])["created"] == 0
    assert not setup[1].items
    dated = replace(ancestor, data=dict(ancestor.data, dates={"not_before": {"day": "2026-10-05"}}))
    assert run(setup, [dated, parent, child])["created"] == 1
    remote = next(iter(setup[1].items.values()))
    assert remote["canBeStartedAt"] == "2026-10-05T00:00:00"
    assert run(setup, [ancestor, parent, child])["removed"] == 1


def test_ancestor_task_dependency_wait_remains_schedulable(setup):
    predecessor = item(1)
    parent = item(2, state="blocked", after=[predecessor.id])
    child = item(3, parent=parent.id)
    assert run(setup, [predecessor, parent, child])["created"] == 2
    one, two = setup[1].items.values()
    assert two["blockedByIds"] == [one["id"]]


def test_first_id_stays_stable_through_append_only_version(setup):
    first = item(1)
    run(setup, [first])
    new = replace(first, id=item(2).id, supersedes=(first.id,), title="Synthetic updated title")
    assert run(setup, [first, new])["updated"] == 1
    assert len(setup[1].items) == 1
    assert set(setup[3].tasks()) == {first.id}
    assert next(iter(setup[1].items.values()))["title"] == new.title


def test_remote_events_and_unowned_tasks_never_enter_snapshot(setup):
    run(setup, [item()])
    setup[1].schedule = [{"itemType": "event", "itemId": "synthetic-event"},
                         {"itemType": "task", "itemId": "synthetic-unowned-task"}]
    run(setup, [item()])
    assert read_snapshot(setup[0].state_file, NOW).blocks == ()


def test_bad_schedule_does_not_replace_snapshot_or_leak_body(setup):
    run(setup, [item()])
    setup[1].schedule = [{"itemType": "task", "itemId": next(iter(setup[1].items)),
                          "startTime": "PRIVATE", "endTime": "PRIVATE"}]
    with pytest.raises(FlowSavvyError) as caught:
        run(setup, [item()])
    assert "PRIVATE" not in str(caught.value)
    assert read_snapshot(setup[0].state_file, NOW).message == "Couldn't see the schedule"


def test_disabled_sync_and_missing_snapshot_do_nothing(tmp_path):
    missing = tmp_path / "not-created.db"
    snapshot = read_snapshot(missing, NOW)
    assert not snapshot.available and not missing.exists()
    result = sync_once(records=None, client=None, state=None,
                       settings=FlowSavvySettings(), now=NOW)
    assert result["enabled"] is False


def test_schema_and_settings_validation(tmp_path):
    from gremlin_town.flowsavvy.settings import ALLOWED_KEYS
    from gremlin_town.runtime import RUNTIME_CONFIG_SCHEMA
    assert RUNTIME_CONFIG_SCHEMA["flowsavvy"] == ALLOWED_KEYS
    for table in ({"enabled": "true"}, {"unrecognized": True}, {"check_seconds": True},
                  {"enabled": True}, {"token_file": ""}, {"max_age_seconds": 0}):
        with pytest.raises(ValueError):
            FlowSavvySettings.from_table(table, base=tmp_path)


def test_watch_honors_wake_during_sync_and_does_not_retry_failure(setup):
    from gremlin_town.flowsavvy.run import watch
    settings, _, _, state = setup
    calls = []
    ticks = []
    def reconcile():
        calls.append(True)
        if len(calls) == 1:
            notify_change(settings)
    watch(settings=settings, state=state, reconcile=reconcile, clock=lambda: len(ticks),
          sleep=lambda seconds: ticks.append(seconds), stop=lambda: len(ticks) >= 3)
    assert len(calls) == 2
    def fail():
        raise FlowSavvyError("synthetic failure")
    with pytest.raises(FlowSavvyError, match="synthetic failure"):
        watch(settings=settings, state=state, reconcile=fail, clock=lambda: 0,
              sleep=lambda _: pytest.fail("Failure must not retry"))


def test_cli_default_off_never_builds_runtime_or_loads_credential(tmp_path, monkeypatch, capsys):
    from gremlin_town.entrypoint import main
    import gremlin_town.runtime as runtime
    config = tmp_path / "config.toml"
    config.write_text("[flowsavvy]\nenabled = false\n")
    monkeypatch.setattr(runtime, "build_runtime", lambda *args, **kwargs: pytest.fail("disabled runtime"))
    assert main(["flowsavvy", "sync", "--config", str(config)]) == 0
    assert json.loads(capsys.readouterr().out) == {"enabled": False}


def test_enabled_cli_uses_real_brain_reader_and_only_read_methods(setup, tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from gremlin_town.entrypoint import main
    import gremlin_town.runtime as runtime
    import gremlin_town.flowsavvy.run as runner
    settings, fake, client, _ = setup
    config = tmp_path / "config.toml"
    config.write_text("[flowsavvy]\nenabled = true\n" + "\n".join(
        f'{key} = {json.dumps(str(getattr(settings, key)).replace(chr(92), "/"))}'
        for key in ("state_file", "token_file", "list_id", "admin_hours_id", "deep_focus_hours_id", "errands_hours_id")))
    settings.token_file.write_text("synthetic-key")
    calls = []
    class ReaderOnly:
        def snapshot_records(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(records=(SimpleNamespace(record=item(), path="synthetic.md"),))
    monkeypatch.setattr(runtime, "build_runtime", lambda *a, **kw: SimpleNamespace(
        brain=object(), review_why_log=None, close=lambda: None))
    monkeypatch.setattr(runtime, "_cached_brain_storage", lambda _: (ReaderOnly(), None))
    monkeypatch.setattr(runner, "FlowSavvyClient", lambda token: client)
    assert main(["flowsavvy", "sync", "--config", str(config)]) == 0
    assert len(fake.items) == 1
    assert calls[0]["kinds"] == frozenset({"item"})
    output = capsys.readouterr().out
    assert json.loads(output)["created"] == 1
    assert item().id not in output and item().title not in output and "synthetic-key" not in output


def test_failed_brain_scan_sends_nothing_and_invalidates_existing_snapshot(setup, tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace
    from gremlin_town.entrypoint import main
    import gremlin_town.runtime as runtime
    import gremlin_town.flowsavvy.run as runner
    settings, fake, client, _ = setup
    run(setup, [item()])
    before = len(fake.calls)
    settings.token_file.write_text("synthetic-key")
    config = tmp_path / "config.toml"
    config.write_text("[flowsavvy]\nenabled = true")
    monkeypatch.setattr(runner.FlowSavvySettings, "from_table", lambda *a, **kw: settings)
    class FailedReader:
        def snapshot_records(self, **kwargs):
            raise RuntimeError("PRIVATE record text")
    monkeypatch.setattr(runtime, "build_runtime", lambda *a, **kw: SimpleNamespace(
        brain=object(), review_why_log=None, close=lambda: None))
    monkeypatch.setattr(runtime, "_cached_brain_storage", lambda _: (FailedReader(), None))
    monkeypatch.setattr(runner, "FlowSavvyClient", lambda token: client)
    assert main(["flowsavvy", "sync", "--config", str(config)]) == 2
    assert len(fake.calls) == before
    assert not read_snapshot(settings.state_file, NOW).available
    output = capsys.readouterr()
    assert "PRIVATE" not in output.err
    assert "no task changes were sent" in output.err


def test_runtime_default_off_and_rejects_unknown_flowsavvy_keys(tmp_path):
    from tests.integration.test_mcp_runtime import mcp_config
    from gremlin_town.runtime import build_runtime
    from gremlin_town.transport import FakeTransport
    config, _ = mcp_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    assert runtime.flowsavvy.enabled is False
    runtime.close()
    with config.open("a") as stream:
        stream.write("\n[flowsavvy]\nunknown = true\n")
    with pytest.raises(ValueError, match="known keys"):
        build_runtime(config, provider=FakeTransport(), start_worker=False)


@pytest.mark.parametrize("minutes,estimate,padded,expected", [
    (1439, "nic", None, 1), (1440, "nic", None, 0), (2880, "nic", None, 0),
    (959, "agent:test", None, 1), (960, "agent:test", None, 0),
    (900, "agent:test", 1439, 1), (900, "agent:test", 1440, 0),
])
def test_long_task_is_left_unscheduled_without_blocking_other_work(setup, minutes, estimate, padded, expected):
    timing = {"duration_minutes": minutes, "estimated_by": estimate}
    if padded is not None:
        timing["padded_minutes"] = padded
    long = replace(item(), data={**item().data, "timing": timing})
    unrelated = item(2)
    before = deepcopy((long, unrelated))
    assert run(setup, before)["created"] == 1 + expected
    assert (long, unrelated) == before
    assert (long.id in setup[3].tasks()) == bool(expected)
    assert unrelated.id in setup[3].tasks()


@pytest.mark.parametrize("explicit", [False, True])
def test_oversized_prior_task_and_dependents_removed_but_other_task_keeps_scheduling(setup, explicit):
    parent = item(10, called="project" if explicit else "task")
    first = item(1, parent=parent.id, order=1)
    second = item(2, parent=parent.id, order=2, **({"after": [first.id]} if explicit else {}))
    third = item(3, after=[second.id])
    unrelated = item(4)
    assert run(setup, [parent, first, second, third, unrelated])["created"] == 4
    remote = setup[3].tasks()[unrelated.id]["remote"]
    changed = replace(first, data={**first.data, "timing": {"duration_minutes": 1440, "estimated_by": "nic"}})
    result = run(setup, [parent, changed, second, third, unrelated])
    assert result["removed"] == 3 and result["created"] == 0
    assert list(setup[1].items) == [remote]
    assert setup[3].tasks()[unrelated.id]["remote"] == remote
    assert read_snapshot(setup[0].state_file, NOW).available
    # A later real split/shortening permits the next fresh sync; no invented parts.
    assert run(setup, [parent, first, second, third, unrelated])["created"] == 3


@pytest.mark.parametrize("minutes", [30, 1440])
def test_canonical_repeat_routine_is_excluded_from_task_scheduling(setup, minutes):
    routine = item(minutes=minutes, repeat="daily")
    assert run(setup, [routine])["created"] == 0
    assert not setup[1].items
