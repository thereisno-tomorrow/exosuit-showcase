from collections import Counter
import anyio
from brain_core import work
from tests.review_card.test_card import setup, NOW

def test_question_composition_builds_one_index_no_weekly_or_day(tmp_path, monkeypatch):
    engine, state, fake, log, store = setup(tmp_path)
    counts = Counter()
    original = work.index_things
    def counted(*args, **kwargs):
        counts['index'] += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(work, 'index_things', counted)
    try:
        async def read():
            return await engine.backend.fresh(NOW, full_day=False)
        view = anyio.run(read)
        assert counts['index'] == 1
        assert 'weekly' not in view
        assert view['day'] == {}
    finally:
        store.close()
        log.close()


def test_details_are_built_on_demand_from_shared_canonical_index(tmp_path):
    from dataclasses import replace
    from brain_core.dashboard import project_work
    from brain_core.review import ItemRecord
    from tests.review_card.test_dependencies import make_chain
    engine, rows, log, store = make_chain(tmp_path)
    # Three levels exercise inherited facts, descendants and nested step cards.
    rows = [rows[0], *(replace(row, data={**row.data, "after": [], "parent": rows[i - 1].id})
                              for i, row in enumerate(rows[1:], 1))]
    engine.runtime.review_sources = replace(engine.runtime.review_sources,
        brain_items=lambda: tuple(ItemRecord(row, "synthetic/" + row.id + ".md") for row in rows))
    try:
        async def read():
            return await engine.backend.fresh(NOW, full_day=False)
        view = anyio.run(read)
        details = view["details"]
        assert not details.built
        ref = rows[0].id
        assert ref in details
        assert not details.built, "Membership must not build a detail"
        actual = details[ref]
        # Compare against the original FULL projector, not a second sliced build.
        expected = project_work(details.session.entries, NOW,
            history=details.session.log.history(), today=NOW.date())["details"][ref]
        def compare(actual, expected):
            for key, value in expected.items():
                if key == "reasons":  # Ordinary cards intentionally omit unused ranking.
                    continue
                if key == "steps":
                    assert len(actual[key]) == len(value)
                    for child, expected_child in zip(actual[key], value):
                        compare(child, expected_child)
                else:
                    assert actual[key] == value
        compare(actual, expected)
        assert actual["steps"][0]["steps"][0]["ref"] == rows[2].id
    finally:
        store.close()
        log.close()


def test_day_destination_extends_same_tap_and_reads_only_missing_inputs(tmp_path, monkeypatch):
    from tests.review_card.test_latency import counted_sources
    engine, state, fake, log, store = setup(tmp_path)
    counts, windows = counted_sources(engine)
    builds = Counter()
    original = work.index_things
    def counted(*args, **kwargs):
        builds["index"] += 1
        return original(*args, **kwargs)
    monkeypatch.setattr(work, "index_things", counted)
    queries = []
    log._connection.set_trace_callback(queries.append)
    async def read():
        task = await engine.backend.fresh(NOW, full_day=False)
        query_count = len(queries)
        day = await engine.backend.extend(task, full_day=True)
        assert day["_read_session"] is task["_read_session"]
        assert day["parts"] is task["parts"]
        assert day["index"] is task["index"]
        assert day["day"]["timeline_start"].startswith(str(NOW.date()) + "T00:00")
        assert len(queries) == query_count, "Extension must not read/reconcile history again"
        again = await engine.backend.extend(day, full_day=True)
        assert again["day"] is day["day"]
        assert "weekly" not in again
    try:
        anyio.run(read)
        assert counts == dict(items=1, preferences=1, calendar=1)
        assert builds["index"] == 1
        assert len(windows) == 1
        assert sum(query == "SELECT * FROM surfacings ORDER BY surfaced_at, id" for query in queries) == 1
    finally:
        log._connection.set_trace_callback(None)
        store.close()
        log.close()


def test_log_snapshot_preserves_filters_and_is_released_before_live_reconciliation(tmp_path):
    engine, state, fake, log, store = setup(tmp_path)
    try:
        expected_general, expected_morning = log.history(), log.morning_history()
        snapshot = log.read_view(NOW.date())
        assert snapshot.history() == expected_general
        assert snapshot.morning_history() == expected_morning
        assert not log._connection.in_transaction
        before = snapshot.history()
        ref = next(iter(work.index_things(entry.record for entry in engine.runtime.review_sources.brain_items())))
        log.set_review_picks((ref,), (ref,), now=NOW, operation_id="snapshot-fixture", review_day=NOW.date())
        assert snapshot.history() == before
        assert log.read_view(NOW.date()).history() != before
        assert not log._connection.in_transaction
    finally:
        store.close()
        log.close()


def test_weekly_build_only_when_requested_without_new_record_or_history_reads(tmp_path, monkeypatch):
    import brain_core.weekly as weekly
    engine, state, fake, log, store = setup(tmp_path)
    original, calls = weekly.weekly_review, []
    def counted(*args, **kwargs):
        calls.append(kwargs.get("prepared_index"))
        return original(*args, **kwargs)
    monkeypatch.setattr(weekly, "weekly_review", counted)
    async def read():
        task = await engine.backend.fresh(NOW, full_day=False)
        assert not calls
        week = await engine.backend.extend(task, weekly=True)
        assert len(calls) == 1 and calls[0] is task["index"]
        assert week["day"] == {}
        assert "weekly" in week
        assert (await engine.backend.extend(week, weekly=True))["weekly"] is week["weekly"]
        assert len(calls) == 1
    try:
        anyio.run(read)
    finally:
        store.close()
        log.close()


def test_runtime_pinned_capability_is_used_once_and_never_reused_across_taps(tmp_path):
    from types import SimpleNamespace
    engine, state, fake, log, store = setup(tmp_path)
    original = engine.runtime.review_sources
    entries = original.brain_items()
    calls = []
    def pinned():
        calls.append(True)
        return SimpleNamespace(items=entries, preferences=(), revision=len(calls))
    engine.runtime.review_read_snapshot = pinned
    def forbidden():
        raise AssertionError("Separate item/preference readers must not run")
    from dataclasses import replace
    engine.runtime.review_sources = replace(original, brain_items=forbidden, brain_preferences=forbidden)
    async def read():
        first = await engine.backend.fresh(NOW, full_day=False)
        assert first["_read_session"].revision == 1
        await engine.backend.extend(first, full_day=True)
        assert len(calls) == 1
        second = await engine.backend.fresh(NOW, full_day=False)
        assert second["_read_session"].revision == 2
        assert first["_read_session"] is not second["_read_session"]
        state["allowed"] = False
        from gremlin_town.review_card.models import CardError
        try:
            await engine.backend.fresh(NOW, full_day=False)
        except CardError:
            pass
        else:
            raise AssertionError("Revoked grant must refuse before reading")
        assert len(calls) == 2
    try:
        anyio.run(read)
    finally:
        store.close()
        log.close()


def test_waiting_navigation_from_opening_does_not_rebuild_day(tmp_path):
    from tests.review_card.test_card import tap, content
    from tests.review_card.test_latency import counted_sources
    engine, state, fake, log, store = setup(tmp_path)
    async def run():
        from gremlin_town.mcp.brain_runtime import WorkRequest
        from tests.review_card.test_card import WORK
        await engine.backend.change(WorkRequest('read-wait-fixture', 'Fictional delivery reply; waiting for Fictional Pat delivery reply', 'owner', 'direct', (
            {'move':'new','title':'Fictional delivery reply'},
            {'move':'wait','thing':'#1','kind':'needs','who':'Fictional Pat','what':'delivery reply'})), owner_id=WORK.owner_id)
        opening = await tap(engine, await engine.open(), 'More options…')
        counts, windows = counted_sources(engine)
        waiting = await tap(engine, opening, "Update something waiting\u2026")
        assert "waiting" in content(waiting)
        assert counts == dict(items=1, preferences=1)
        assert not windows
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()


def test_production_runtime_assembles_pinned_reader_selected_by_card(tmp_path, monkeypatch):
    from gremlin_town import runtime as runtime_module
    from gremlin_town.transport import FakeTransport
    from tests.integration.test_mcp_runtime import mcp_config
    from tests.review.test_brain_source import FakeSource, REVISION
    engine, state, fake, log, store = setup(tmp_path / "card")
    entries = engine.runtime.review_sources.brain_items()
    class Source(FakeSource):
        def __init__(self):
            super().__init__(tuple(entry.record for entry in entries))
            self.head_reads = 0
            self.pins = []
        def revision(self):
            self.head_reads += 1
            return REVISION
        def snapshot_records(self, *, revision=None, **kwargs):
            self.pins.append(revision)
            assert revision is REVISION
            return super().snapshot_records(**kwargs)
    source = Source()
    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", lambda brain: source)
    config, _ = mcp_config(tmp_path)
    assembled = runtime_module.build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert callable(assembled.review_read_snapshot)
        engine.runtime.review_read_snapshot = assembled.review_read_snapshot
        from dataclasses import replace
        def forbidden():
            raise AssertionError("Unpinned fallback must not run")
        engine.runtime.review_sources = replace(engine.runtime.review_sources,
            brain_items=forbidden, brain_preferences=forbidden)
        async def read():
            return await engine.backend.fresh(NOW, full_day=False)
        view = anyio.run(read)
        assert view["_read_session"].revision is REVISION
        assert source.head_reads == 1 and source.pins == [REVISION, REVISION]
    finally:
        assembled.close()
        store.close()
        log.close()


def test_failed_extension_refuses_safely_and_next_host_tap_survives(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from gremlin_town.review_card.host import InteractionHost
    from tests.review_card.test_host import Engine, Client, settings, event
    engine, state, fake, log, store = setup(tmp_path)
    async def run():
        view = await engine.backend.fresh(NOW, full_day=False)
        compose = engine.backend._compose
        def unavailable(*args, **kwargs):
            raise RuntimeError("PRIVATE provider failure")
        monkeypatch.setattr(engine.backend, "_compose", unavailable)
        class ExtendingEngine(Engine):
            async def handle(self, *args, **kwargs):
                await engine.backend.extend(view, full_day=True)
                return SimpleNamespace(message={"components": []})
        calls = []
        client = Client(calls)
        client.followup = lambda *args: calls.append(("refusal", args[-1]))
        host = InteractionHost(ExtendingEngine(calls), client, settings())
        try:
            assert not await host.interaction(event())
            assert any(isinstance(row, tuple) and row[0] == "refusal" for row in calls)
            assert "PRIVATE" not in str(calls)
            monkeypatch.setattr(engine.backend, "_compose", compose)
            assert await host.interaction(event())
            assert "edit" in calls
        finally:
            host.close()
    try:
        anyio.run(run)
    finally:
        store.close()
        log.close()



def test_bulk_detail_iteration_builds_once_and_preserves_selected_detail(tmp_path, monkeypatch):
    from gremlin_town.review_card import reads
    from tests.review_card.test_dependencies import make_chain
    engine, rows, log, store = make_chain(tmp_path)
    calls = []
    original = reads.project_work
    def counted(*args, **kwargs):
        calls.append(kwargs.get("detail_refs"))
        return original(*args, **kwargs)
    monkeypatch.setattr(reads, "project_work", counted)
    async def read():
        view = await engine.backend.fresh(NOW, full_day=False)
        details = view["details"]
        selected = details[rows[-1].id]
        assert calls == [(rows[-1].id,)]
        assert len(list(details.values())) == len(rows)
        assert calls == [(rows[-1].id,), None]
        assert list(dict(details.items())) == list(view["index"])
        assert dict(details.items())[rows[-1].id] is selected
        assert len(list(details.values())) == len(rows)
        assert len(calls) == 2
    try:
        anyio.run(read)
    finally:
        store.close()
        log.close()
