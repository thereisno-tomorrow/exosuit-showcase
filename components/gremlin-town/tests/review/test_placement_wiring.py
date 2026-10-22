"""The placement runner wired in (step I; build plan slice 4): the 3-minute poll runs it last, the
signal route takes day_settled and day_change and answers with the plan, and the config checks
[placement]."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gremlin_town.review.collect import ReviewSources
from gremlin_town.review.placement import PlacementContext
from gremlin_town.review.poll import poll_once
from gremlin_town.review.run import ReviewContext
from gremlin_town.review.settings import PlacementSettings, PlacementSettingsError
from gremlin_town.review.why_log import WhyLog
from gremlin_town.runtime import build_runtime
from gremlin_town.transport import FakeTransport

from test_api_routes import _headers, write_config
from test_poll import FakeEventsService, enabled_settings

NOW = datetime(2026, 9, 26, 4, 0, tzinfo=timezone.utc)


def test_poll_runs_placement_last_and_only_when_on(tmp_path: Path) -> None:
    why_log = WhyLog(tmp_path / "why.sqlite3")
    base = dict(settings=enabled_settings(str(tmp_path / "why.sqlite3")), why_log=why_log, sources=ReviewSources(),
                service=FakeEventsService({}))
    off = PlacementContext(settings=PlacementSettings(), why_log=why_log, sources=ReviewSources())
    assert poll_once(ReviewContext(**base, placement=off), NOW) == ()
    on = PlacementContext(settings=PlacementSettings(enabled=True), why_log=why_log, sources=ReviewSources())
    assert poll_once(ReviewContext(**base, placement=on), NOW) == ({"placement": {"outcome": "not_settled"}},)


def _placement_config(root: Path, placement: list[str]) -> Path:
    config = write_config(root, review_enabled=True)
    config.write_text(config.read_text(encoding="utf-8") + "\n" + "\n".join(["[placement]", *placement]), encoding="utf-8")
    return config


def test_day_settled_is_recorded_and_answered_with_the_placement_run(tmp_path: Path) -> None:
    runtime = build_runtime(_placement_config(tmp_path, ["enabled = true"]), provider=FakeTransport(), start_worker=False)
    try:
        with TestClient(runtime.app) as client:
            response = client.post("/v1/moments/signals", headers=_headers("hermes-wa"), json={"kind": "day_settled"})
        assert response.status_code == 200
        body = response.json()
        assert body["kind"] == "day_settled" and body["placement"] == {"outcome": "not_configured"}  # no Brain Home here
    finally:
        runtime.close()


def test_a_malformed_day_change_is_refused_and_not_recorded(tmp_path: Path) -> None:
    runtime = build_runtime(_placement_config(tmp_path, ["enabled = true"]), provider=FakeTransport(), start_worker=False)
    try:
        with TestClient(runtime.app) as client:
            bad = client.post("/v1/moments/signals", headers=_headers("hermes-wa"),
                              json={"kind": "day_change", "detail": '{"lunch": 30}'})
            good = client.post("/v1/moments/signals", headers=_headers("hermes-wa"),
                               json={"kind": "day_change", "detail": '{"ends_at": "16:00", "words": "done at 4"}'})
            rows = runtime.review_why_log._connection.execute("SELECT kind, detail FROM signals").fetchall()
        assert bad.status_code == 422 and good.status_code == 200
        assert [(row["kind"], row["detail"]) for row in rows] == [("day_change", '{"ends_at": "16:00", "words": "done at 4"}')]
    finally:
        runtime.close()


def test_signals_answer_as_before_when_placement_is_off(tmp_path: Path) -> None:
    runtime = build_runtime(write_config(tmp_path, review_enabled=True), provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.placement_context is None
        with TestClient(runtime.app) as client:
            body = client.post("/v1/moments/signals", headers=_headers("hermes-wa"), json={"kind": "day_settled"}).json()
        assert "placement" not in body
    finally:
        runtime.close()


def test_writing_needs_the_calendar_writer(tmp_path: Path) -> None:
    with pytest.raises(Exception, match="calendar.events"):
        build_runtime(_placement_config(tmp_path, ["enabled = true", "write = true"]), provider=FakeTransport(), start_worker=False)


def test_placement_needs_review(tmp_path: Path) -> None:
    config = write_config(tmp_path, review_enabled=False)
    config.write_text(config.read_text(encoding="utf-8") + "\n[placement]\nenabled = true\n", encoding="utf-8")
    with pytest.raises(PlacementSettingsError):
        build_runtime(config, provider=FakeTransport(), start_worker=False)



def test_pc_on_keeps_taking_any_detail(tmp_path: Path) -> None:
    runtime = build_runtime(_placement_config(tmp_path, ["enabled = true"]), provider=FakeTransport(), start_worker=False)
    try:
        with TestClient(runtime.app) as client:
            response = client.post("/v1/moments/signals", headers=_headers("local-codex"), json={"kind": "pc_on", "detail": 7})
        assert response.status_code == 200
    finally:
        runtime.close()


def test_a_placement_failure_after_the_signal_is_stored_says_the_day_was_not_planned(
    tmp_path: Path, monkeypatch,
) -> None:
    """A run that raised says so (plan_failed, with why in plain words), never plan_pending, which now means
    only that the run is still going (the storage-limit plan, layer 1; the review of fix/diablo-speed-tokens)."""

    import gremlin_town.review.placement as placement_module
    from gremlin_town.api.review import PLAN_FAILED_WORDS

    def broken(context, now):
        raise RuntimeError("boom")

    monkeypatch.setattr(placement_module, "run_placement", broken)
    runtime = build_runtime(_placement_config(tmp_path, ["enabled = true"]), provider=FakeTransport(), start_worker=False)
    try:
        with TestClient(runtime.app) as client:
            response = client.post("/v1/moments/signals", headers=_headers("hermes-wa"), json={"kind": "day_settled"})
            rows = runtime.review_why_log._connection.execute("SELECT kind FROM signals").fetchall()
        assert response.status_code == 200
        assert response.json()["placement"] == {"outcome": "plan_failed", "error": PLAN_FAILED_WORDS}
        assert [row["kind"] for row in rows] == ["day_settled"]
    finally:
        runtime.close()


def test_the_placement_calendar_reader_is_strict(monkeypatch) -> None:
    from gremlin_town.review.collect import SourceUnavailable
    from gremlin_town.runtime import _google_calendar_reader
    from gremlin_town.sources.google.calendar import CalendarReader
    from gremlin_town.sources.google.settings import GoogleSettings
    from gremlin_town.sources.result import SourceRead

    event = object()
    monkeypatch.setattr(CalendarReader, "events", lambda self, start, end: SourceRead.partial(items=(event,), unavailable=("work",)))
    monkeypatch.setattr("gremlin_town.runtime._google_calendar_event", lambda item: item)
    google = GoogleSettings.from_table({"enabled": True, "account": "nic@example.test", "credential_env": "X"})
    lenient = _google_calendar_reader(google, (None, object()))
    strict = _google_calendar_reader(google, (None, object()), strict=True)
    # Read-only surfaces can retain usable events while displaying the partial warning.
    with pytest.raises(SourceUnavailable, match="partial") as partial:
        lenient(NOW, NOW)
    assert partial.value.items == (event,)
    # Placement cannot plan over an unread calendar, even when another calendar worked.
    with pytest.raises(SourceUnavailable, match="partial") as refused:
        strict(NOW, NOW)
    assert refused.value.items == ()


# --- The inline run's cap (finding 6 of the 30 Sep 2026 fixes plan; approved by Nic, 30 Sep) -----


def _wait_for(condition, seconds: float = 5.0) -> None:
    import time as time_module

    deadline = time_module.monotonic() + seconds
    while not condition():
        if time_module.monotonic() > deadline:
            raise AssertionError("timed out waiting")
        time_module.sleep(0.01)


def _slow_day(tmp_path: Path, monkeypatch):
    """A hub whose signal route runs a real day's placement (Thursday 1 Oct 2026, one pick) with the
    route's clock at 06:32 Singapore time and its cap cut to 0.05 s."""

    import gremlin_town.api.review as review_api
    from test_placement_run import FakeWriter, at, item, pick

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return at("06:32").astimezone(tz)

    monkeypatch.setattr(review_api, "datetime", Clock)
    monkeypatch.setattr(review_api, "INLINE_PLACEMENT_SECONDS", 0.05)
    runtime = build_runtime(_placement_config(tmp_path, ["enabled = true"]), provider=FakeTransport(), start_worker=False)
    bank = item("bank")
    writer = FakeWriter()
    runtime.placement_context = PlacementContext(
        settings=PlacementSettings(enabled=True, write=True), why_log=runtime.review_why_log,
        sources=ReviewSources(
            brain_items=lambda: (bank,), calendar_events=lambda start, end: (), brain_preferences=lambda: (),
        ),
        writer=writer, history=lambda: (pick(bank, at("06:30")),),
    )
    poll_context = ReviewContext(
        settings=enabled_settings(str(tmp_path / "why.sqlite3")), why_log=runtime.review_why_log,
        sources=ReviewSources(), service=FakeEventsService({}), placement=runtime.placement_context,
    )
    return runtime, writer, bank, poll_context


def test_a_slow_day_settle_replies_plan_pending_and_the_poll_draws_the_settled_day(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    """Past the cap the reply says plan_pending at once. The day is settled all the same (the signal
    was stored first), and when the run can't finish, the 3-minute poll draws the day."""

    import threading
    import time as time_module

    import gremlin_town.review.placement as placement_module
    from test_placement_run import DAY, at

    runtime, writer, bank, poll_context = _slow_day(tmp_path, monkeypatch)
    real_run = placement_module.run_placement
    release = threading.Event()

    def stalls_then_dies(context, now):
        release.wait(5)
        raise RuntimeError("the hub restarted mid-run")

    monkeypatch.setattr(placement_module, "run_placement", stalls_then_dies)
    try:
        # The why-log closes with the app, so every check runs while it is up.
        with TestClient(runtime.app) as client:
            started = time_module.monotonic()
            response = client.post("/v1/moments/signals", headers=_headers("hermes-wa"), json={"kind": "day_settled"})
            replied_in = time_module.monotonic() - started
            release.set()
            _wait_for(lambda: "failed after its reply" in caplog.text)
            assert response.status_code == 200 and response.json()["placement"] == {"outcome": "plan_pending"}
            assert replied_in < 3
            assert len(runtime.review_why_log.signals_on("day_settled", DAY)) == 1
            assert writer.entries == {} and runtime.review_why_log.latest_placement(DAY) is None

            monkeypatch.setattr(placement_module, "run_placement", real_run)
            results = poll_once(poll_context, at("06:35"))
            assert results[-1]["placement"]["outcome"] == "written"
            assert set(writer.times()) == {"block admin:0900", f"slot {bank.record.id}:1@0900"}
    finally:
        runtime.close()


def test_a_slow_run_finishes_after_its_reply_and_records_the_plan(tmp_path: Path, monkeypatch, caplog) -> None:
    import threading

    import gremlin_town.review.placement as placement_module
    from test_placement_run import DAY, at

    runtime, writer, bank, poll_context = _slow_day(tmp_path, monkeypatch)
    real_run = placement_module.run_placement
    release = threading.Event()

    def slow(context, now):
        release.wait(5)
        return real_run(context, now)

    monkeypatch.setattr(placement_module, "run_placement", slow)
    try:
        with TestClient(runtime.app) as client:
            response = client.post("/v1/moments/signals", headers=_headers("hermes-wa"), json={"kind": "day_settled"})
            assert writer.entries == {}  # still running when the reply went out
            release.set()
            _wait_for(lambda: "finished after its reply" in caplog.text)
            assert response.json()["placement"] == {"outcome": "plan_pending"}
            assert set(writer.times()) == {"block admin:0900", f"slot {bank.record.id}:1@0900"}
            assert runtime.review_why_log.latest_placement(DAY)["outcome"] == "written"
            monkeypatch.setattr(placement_module, "run_placement", real_run)
            assert poll_once(poll_context, at("06:35"))[-1]["placement"]["outcome"] == "unchanged"
    finally:
        runtime.close()


def test_a_run_inside_the_cap_answers_with_the_plan(tmp_path: Path, monkeypatch) -> None:
    import gremlin_town.api.review as review_api

    runtime, writer, bank, _poll_context = _slow_day(tmp_path, monkeypatch)
    monkeypatch.setattr(review_api, "INLINE_PLACEMENT_SECONDS", 12.0)
    try:
        with TestClient(runtime.app) as client:
            response = client.post("/v1/moments/signals", headers=_headers("hermes-wa"), json={"kind": "day_settled"})
        placed = response.json()["placement"]
        assert placed["outcome"] == "written" and placed["summary"][0].startswith("Today: admin")
        assert "seconds" not in placed  # the timing stays on the hub; Diablo's reply is unchanged
    finally:
        runtime.close()


def test_the_inline_cap_is_twelve_seconds_under_diablos_twenty() -> None:
    import gremlin_town.api.review as review_api
    from adapters.hermes.hub_client import HubClientConfig

    assert review_api.INLINE_PLACEMENT_SECONDS == 12.0
    assert HubClientConfig("https://hub.test", "fixture-token").timeout == 20.0


# --- Review of the branch (30 Sep 2026): the late run holds the lock, and shutdown waits for it -----


def _gated_day(tmp_path: Path, monkeypatch):
    """_slow_day, but the real run_placement runs: it takes the placement lock and then blocks inside its
    calendar read until the gate opens."""

    import threading
    from dataclasses import replace

    runtime, writer, bank, poll_context = _slow_day(tmp_path, monkeypatch)
    gate, entered = threading.Event(), threading.Event()

    def calendar(start, end):
        entered.set()
        gate.wait(5)
        return ()

    runtime.placement_context.sources = replace(runtime.placement_context.sources, calendar_events=calendar)
    return runtime, writer, bank, poll_context, gate, entered


def test_a_late_run_holds_the_lock_so_the_poll_answers_busy_until_it_finishes(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    from test_placement_run import at

    runtime, writer, bank, poll_context, gate, entered = _gated_day(tmp_path, monkeypatch)
    try:
        with TestClient(runtime.app) as client:
            response = client.post("/v1/moments/signals", headers=_headers("hermes-wa"), json={"kind": "day_settled"})
            assert response.json()["placement"] == {"outcome": "plan_pending"}
            assert entered.wait(5)  # the late run is inside the calendar read, holding the lock
            assert poll_once(poll_context, at("06:35"))[-1]["placement"] == {"outcome": "busy"}
            gate.set()
            _wait_for(lambda: "finished after its reply" in caplog.text)
            assert set(writer.times()) == {"block admin:0900", f"slot {bank.record.id}:1@0900"}
            assert poll_once(poll_context, at("06:36"))[-1]["placement"]["outcome"] == "unchanged"
    finally:
        gate.set()
        runtime.close()


def test_shutdown_waits_for_a_late_run_to_record_its_plan_and_free_the_lock(
    tmp_path: Path, monkeypatch, caplog,
) -> None:
    """Uvicorn no longer waits for the run once the request has replied, so the service's shutdown does:
    otherwise the why-log closes under the run after its calendar writes, the plan is never recorded and
    the lock stays held for 300 s."""

    import threading
    from datetime import timedelta

    from test_placement_run import DAY, at

    runtime, writer, bank, _poll_context, gate, entered = _gated_day(tmp_path, monkeypatch)
    try:
        with TestClient(runtime.app) as client:
            response = client.post("/v1/moments/signals", headers=_headers("hermes-wa"), json={"kind": "day_settled"})
            assert response.json()["placement"] == {"outcome": "plan_pending"}
            assert entered.wait(5)
            threading.Timer(0.3, gate.set).start()
        # Leaving the client shut the app down while the run was still in the calendar read.
        assert "shutdown waits up to 20 s for 1 placement run(s) still going" in caplog.text
        assert "at shutdown" not in caplog.text  # it finished inside the bound
        assert set(writer.times()) == {"block admin:0900", f"slot {bank.record.id}:1@0900"}
        after = WhyLog(tmp_path / "why.sqlite3")
        try:
            assert after.latest_placement(DAY)["outcome"] == "written"
            assert after.take_placement_lock("next-run", at("06:40"), stale_after=timedelta(seconds=300))
        finally:
            after.close()
    finally:
        gate.set()
        runtime.close()


def test_shutdown_stops_waiting_for_a_stuck_late_run(tmp_path: Path, monkeypatch, caplog) -> None:
    import threading

    import gremlin_town.api.review as review_api

    monkeypatch.setattr(review_api, "LATE_RUN_SHUTDOWN_SECONDS", 0.2)
    runtime, _writer, _bank, _poll_context, gate, entered = _gated_day(tmp_path, monkeypatch)
    try:
        with TestClient(runtime.app) as client:
            client.post("/v1/moments/signals", headers=_headers("hermes-wa"), json={"kind": "day_settled"})
            assert entered.wait(5)
            threading.Timer(1.0, gate.set).start()
        assert "1 placement run(s) still going after 0 s at shutdown" in caplog.text
    finally:
        gate.set()
        runtime.close()


def test_a_timeout_raised_inside_the_run_is_a_failure_not_a_late_run(tmp_path: Path, monkeypatch, caplog) -> None:
    import gremlin_town.api.review as review_api
    import gremlin_town.review.placement as placement_module
    from gremlin_town.api.review import PLAN_FAILED_WORDS

    def socket_timeout(context, now):
        raise TimeoutError("GitHub read timed out")

    monkeypatch.setattr(placement_module, "run_placement", socket_timeout)
    runtime, _writer, _bank, _poll_context = _slow_day(tmp_path, monkeypatch)
    monkeypatch.setattr(review_api, "INLINE_PLACEMENT_SECONDS", 5.0)
    try:
        with TestClient(runtime.app) as client:
            response = client.post("/v1/moments/signals", headers=_headers("hermes-wa"), json={"kind": "day_settled"})
            # It failed and nothing is coming from this run: plan_failed, not plan_pending (before layer 1 it was).
            assert response.json()["placement"] == {"outcome": "plan_failed", "error": PLAN_FAILED_WORDS}
            assert runtime.placement_late_runs == set()
        assert "placement run after a day_settled signal failed" in caplog.text
        assert "the run goes on" not in caplog.text
    finally:
        runtime.close()
