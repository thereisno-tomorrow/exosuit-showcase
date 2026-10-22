"""Brain Home's size on the review side (docs/product/2026-09-30-brain-home-storage-limit-plan.md, layer 1):
the 06:00 review's "Brain Home size" line from 50% and again from 80% of a limit, and nothing below; a read
past a cap said in plain words; placement's own outcome for a failure that won't clear, apart from a merely
slow run; daytime returns that resolve a pinned path from `archive/` and never bring back a thing archived
whole; a failed read no longer dropped silently; and the read-only `gremlin-town brain-home size` check.

Brain Home reads go through the real `GitHubBrainHomeStorage` against Brain Core's fake GitHub transport."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache, GitHubStorageError, HTTPResponse
from brain_core.records import record_from_mapping, render_record
from brain_core.review import build_review, render_review

from gremlin_town.review.brain_source import brain_home_size_reader, brain_items_by_path_reader, brain_items_reader
from gremlin_town.review.collect import ReviewSources, collect_review_inputs
from gremlin_town.review.run import ReviewContext, run_moments
from gremlin_town.review.why_log import Surfacing, WhyLog
from tests.mcp.test_brain_composition import LANE_B

NOW = datetime(2026, 9, 25, 22, 0, tzinfo=timezone.utc)  # 06:00 SGT on 26 Sep
OVER = "Nothing is lost. Ask for the storage tidy-up now."
TASKS_STOP = "the review, placement, \"where am I?\" and \"what next?\" stop seeing your tasks"
HEADS_UP_LINE = (
    f"your task records are at 2,500 of the 5,000 the hub reads at once. Nothing is lost. Past 5,000, {TASKS_STOP}; "
    "ask for the storage tidy-up (old versions moved into Brain Home's archive) before then."
)
URGENT_LINE = f"your task records are at 4,000 of 5,000. Past 5,000, {TASKS_STOP}. Ask for the storage tidy-up now."
TOO_BIG = (
    "Brain Home has grown too big for GitHub to hand over in one go, so the hub can't read it. Nothing is lost. "
    "Ask for the Brain Home size check."
)


def _storage(remote, *, cached: bool = True) -> GitHubBrainHomeStorage:
    return GitHubBrainHomeStorage(
        repository=LANE_B.REPOSITORY, branch=LANE_B.BRANCH, token=LANE_B.TOKEN, transport=remote,
        object_cache=GitHubObjectCache() if cached else None,
    )


def _item_files(count: int) -> dict[str, bytes]:
    # Counting and the cap read only the tree, so the files' bytes don't matter.
    return {f"records/item/2026/09/brn_20260921_040000_{index:08x}.md": b"x" for index in range(count)}


def _item(record_id: str, *, supersedes: tuple[str, ...] = (), title: str = "File the form") -> tuple[str, bytes]:
    moment = f"{record_id[4:8]}-{record_id[8:10]}-{record_id[10:12]}T{record_id[13:15]}:{record_id[15:17]}:{record_id[17:19]}+00:00"
    record = record_from_mapping({
        "schema": "brain.record/v2", "id": record_id, "kind": "item", "title": title, "state": "open",
        "created_at": moment, "updated_at": moment, "sensitivity": "private",
        "vaults": ["vault://brain-home"], "owners": ["brain-home"], "entities": [],
        "evidence": [{"source_type": "fixture", "locator": "fixture:item",
                      "content_sha256": hashlib.sha256(record_id.encode()).hexdigest(), "excerpt": title}],
        "supersedes": list(supersedes), "dedupe_key": None, "data": {"level": "accepted", "size": "task", "block": "admin"},
        "body": title, "source": {"said_by": "owner", "came_by": "direct", "words": title, "recorded_by": "hermes-wa"},
    }, "storage-limit fixture")
    return f"records/item/{moment[:4]}/{moment[5:7]}/{record_id}.md", render_record(record).encode("utf-8")


def _move_to_archive(remote, *paths: str) -> None:
    """One commit moving each file to archive/ plus its path, byte for byte (the layout layer 2 will use)."""

    tree = dict(remote.trees[remote.commits[remote.branch]["tree"]])
    for path in paths:
        tree["archive/" + path] = tree.pop(path)
    tree_sha = remote._sha()
    remote.trees[tree_sha] = tree
    commit_sha = remote._sha()
    remote.commits[commit_sha] = {"tree": tree_sha, "parent": remote.branch, "message": "archive", "paths": set(paths)}
    remote.branch = commit_sha


# --- The 06:00 review --------------------------------------------------------------------------------------


def test_a_review_past_a_scan_cap_says_so_plainly_and_what_next_still_declines(tmp_path: Path) -> None:
    from gremlin_town.review.orient import OrientationContext, OrientationSources, what_next
    from gremlin_town.review.settings import OrientationSettings

    from test_run import enabled_settings

    remote = LANE_B.FakeGitHub()
    remote.seed(_item_files(3))
    sources = ReviewSources(brain_items=brain_items_reader(_storage(remote), max_records=2))
    sentence = f"Brain Home holds 3 task records and the hub reads at most 2 at once. {OVER}"

    inputs = collect_review_inputs(sources, (), NOW)

    assert inputs.source_status["brain"] == sentence
    assert f"brain: {sentence}" in render_review(build_review(inputs))

    why_log = WhyLog(tmp_path / "why.sqlite3")
    try:
        context = OrientationContext(
            orientation_settings=OrientationSettings.from_table({"enabled": True}, review_enabled=True),
            review_settings=enabled_settings(":memory:"), why_log=why_log,
            sources=OrientationSources(review=sources),
        )
        answer = json.dumps(what_next(context, NOW), default=str)
    finally:
        why_log.close()
    assert "Couldn't see your saved items, so there's nothing safe to recommend yet." in answer
    assert sentence in answer


@pytest.mark.parametrize(("files", "line"), [(2499, None), (2500, HEADS_UP_LINE), (3999, None), (4000, URGENT_LINE)])
def test_the_review_says_brain_home_size_from_half_a_limit_and_again_from_80_percent(files: int, line) -> None:
    remote = LANE_B.FakeGitHub()
    remote.seed(_item_files(files))
    sources = ReviewSources(brain_items=lambda: (), brain_home_size=brain_home_size_reader(_storage(remote)))

    inputs = collect_review_inputs(sources, (), NOW)

    if files == 3999:  # still a heads-up, not yet urgent
        assert inputs.source_status["Brain Home size"].startswith("your task records are at 3,999 of the 5,000")
        return
    assert inputs.source_status.get("Brain Home size") == line
    text = render_review(build_review(inputs))
    assert (f"Brain Home size: {line}" in text) if line else ("Brain Home size" not in text)


def test_the_size_line_is_not_said_twice_when_a_read_already_said_it() -> None:
    remote = LANE_B.FakeGitHub()
    remote.seed(_item_files(5001))
    storage = _storage(remote)
    sources = ReviewSources(
        brain_items=brain_items_reader(storage, max_records=5000, max_bytes=32 * 1024 * 1024),
        brain_home_size=brain_home_size_reader(storage),
    )

    status = collect_review_inputs(sources, (), NOW).source_status

    assert status["brain"] == f"Brain Home holds 5,001 task records and the hub reads at most 5,000 at once. {OVER}"
    assert "Brain Home size" not in status


def test_a_size_check_that_fails_is_said_under_couldnt_see() -> None:
    def too_big():
        raise GitHubStorageError(
            "github_tree_incomplete", "GitHub did not return the complete pinned tree",
            details={"truncated": True, "tree_matches": True},
        )

    status = collect_review_inputs(ReviewSources(brain_items=lambda: (), brain_home_size=too_big), (), NOW).source_status

    assert status["Brain Home size"] == TOO_BIG


def test_a_different_tree_is_not_called_too_big_and_passes() -> None:
    from gremlin_town.review.brain_source import brain_problem

    error = GitHubStorageError(
        "github_tree_incomplete", "GitHub did not return the complete pinned tree",
        details={"truncated": False, "tree_matches": False},
    )

    assert brain_problem(error) == (
        "GitHub answered with a different version of Brain Home than the one asked for, so the hub couldn't read it "
        "this time. Nothing is lost. It should work again shortly.",
        False,
    )


def test_with_facts_the_fullest_scan_reads_tasks_and_facts_together_and_the_line_names_both() -> None:
    """Agents' list reads tasks and facts together, so with any facts it is fuller than tasks alone; search
    counts the same files plus captures and commitments, of which none are live, so it ties and the first
    listed, agents' list, is named (the docs' example says the same)."""

    remote = LANE_B.FakeGitHub()
    remote.seed({
        **_item_files(2500),
        **{f"records/fact/2026/09/brn_20260921_050000_{index:08x}.md": b"x" for index in range(100)},
    })

    status = collect_review_inputs(
        ReviewSources(brain_items=lambda: (), brain_home_size=brain_home_size_reader(_storage(remote))), (), NOW,
    ).source_status

    assert status["Brain Home size"] == (
        "your task and fact records are at 2,600 of the 5,000 the hub reads at once. Nothing is lost. Past 5,000, "
        "agents can't list your tasks; ask for the storage tidy-up (old versions moved into Brain Home's archive) "
        "before then."
    )


def test_past_the_preference_limit_placement_plans_nothing_and_the_line_says_so() -> None:
    from brain_core.capacity import scan_headroom

    from gremlin_town.review.brain_source import HUB_SCANS

    [preferences] = [scan for scan in HUB_SCANS if scan.kinds == frozenset({"preference"})]
    counts = SimpleNamespace(live_files={"preference": 4000}, live_bytes={}, tree_entries=0)

    assert scan_headroom(preferences, counts).sentence == (
        "your preference records are at 4,000 of 5,000. Past 5,000, placement plans nothing and your focus "
        "projects stop counting. Ask for the storage tidy-up now."
    )


def test_only_the_0600_review_says_brain_home_size(tmp_path: Path) -> None:
    from gremlin_town.review.orient import OrientationContext, OrientationSources, _collect
    from gremlin_town.review.settings import OrientationSettings

    from test_run import enabled_settings

    def never():
        raise AssertionError("the asks never read Brain Home's size")

    why_log = WhyLog(tmp_path / "why.sqlite3")
    try:
        context = OrientationContext(
            orientation_settings=OrientationSettings.from_table({"enabled": True}, review_enabled=True),
            review_settings=enabled_settings(":memory:"), why_log=why_log,
            sources=OrientationSources(review=ReviewSources(brain_items=lambda: (), brain_home_size=never)),
        )
        status = _collect(context, (), NOW).review.source_status
    finally:
        why_log.close()
    assert "Brain Home size" not in status


# --- Placement: a failure that won't clear has its own outcome; a slow run stays plan_pending ----------


def test_day_settled_past_a_cap_says_the_day_was_not_planned_and_why(tmp_path: Path, monkeypatch, caplog) -> None:
    from fastapi.testclient import TestClient

    import gremlin_town.api.review as review_api
    from gremlin_town.review.poll import poll_once

    from test_api_routes import _headers
    from test_placement_run import at
    from test_placement_wiring import _slow_day

    runtime, writer, _bank, poll_context = _slow_day(tmp_path, monkeypatch)
    monkeypatch.setattr(review_api, "INLINE_PLACEMENT_SECONDS", 12.0)
    remote = LANE_B.FakeGitHub()
    remote.seed(_item_files(3))
    runtime.placement_context.sources = replace(
        runtime.placement_context.sources, brain_items=brain_items_reader(_storage(remote), max_records=2),
    )
    expected = {
        "outcome": "cannot_read_brain_home",
        "error": f"Brain Home holds 3 task records and the hub reads at most 2 at once. {OVER}",
    }
    try:
        with TestClient(runtime.app) as client:
            response = client.post("/v1/moments/signals", headers=_headers("hermes-wa"), json={"kind": "day_settled"})
            assert response.status_code == 200 and response.json()["placement"] == expected
            polled = poll_once(poll_context, at("06:35"))[-1]["placement"]
            assert {key: polled[key] for key in ("outcome", "error")} == expected
        assert writer.entries == {}
        assert "placement run failed" not in caplog.text  # said plainly, not a traceback every 3 minutes
        assert "placement couldn't read Brain Home and planned nothing" in caplog.text
    finally:
        runtime.close()


class _AllowanceUsedUp:
    def request(self, method, url, *, headers, body):  # type: ignore[no-untyped-def]
        return HTTPResponse(403, {"x-ratelimit-remaining": "0"}, b'{"message": "rate limited"}')


def test_day_settled_when_githubs_allowance_is_used_up_says_plan_failed_with_why(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    import gremlin_town.api.review as review_api

    from test_api_routes import _headers
    from test_placement_wiring import _slow_day

    runtime, _writer, _bank, _poll_context = _slow_day(tmp_path, monkeypatch)
    monkeypatch.setattr(review_api, "INLINE_PLACEMENT_SECONDS", 12.0)
    runtime.placement_context.sources = replace(
        runtime.placement_context.sources, brain_items=brain_items_reader(_storage(_AllowanceUsedUp())),
    )
    try:
        with TestClient(runtime.app) as client:
            response = client.post("/v1/moments/signals", headers=_headers("hermes-wa"), json={"kind": "day_settled"})
        assert response.json()["placement"] == {
            "outcome": "plan_failed",
            "error": "GitHub's hourly allowance for Brain Home is used up for now. Nothing is lost; it refills within the hour.",
        }
    finally:
        runtime.close()


# --- Daytime returns: a pinned path in archive/, a thing archived whole, and a failed read ----------------


OLD_ID, NEW_ID = "brn_20260926_010000_aaaa1111", "brn_20260926_020000_bbbb2222"


def _two_versions(remote) -> str:
    old_path, old_bytes = _item(OLD_ID)
    new_path, new_bytes = _item(NEW_ID, supersedes=(OLD_ID,), title="File the form today")
    remote.seed({old_path: old_bytes, new_path: new_bytes})
    return old_path


def test_a_pinned_path_now_in_the_archive_returns_the_same_item_and_a_thing_archived_whole_nothing() -> None:
    remote = LANE_B.FakeGitHub()
    old_path = _two_versions(remote)
    read = brain_items_by_path_reader(_storage(remote))
    before = read((old_path,))

    _move_to_archive(remote, old_path)
    after = read((old_path,))

    assert [(entry.record.id, entry.path, entry.record.as_dict()) for entry in after] == [
        (entry.record.id, entry.path, entry.record.as_dict()) for entry in before
    ]
    assert [entry.record.id for entry in after] == [OLD_ID]

    alone = LANE_B.FakeGitHub()
    path, content = _item(OLD_ID)
    alone.seed({path: content})
    _move_to_archive(alone, path)  # test data, archived whole: no current record names it
    assert brain_items_by_path_reader(_storage(alone))((path,)) == ()


def test_a_two_version_thing_archived_whole_stays_hidden() -> None:
    remote = LANE_B.FakeGitHub()
    old_path = _two_versions(remote)
    new_path = _item(NEW_ID, supersedes=(OLD_ID,), title="File the form today")[0]
    _move_to_archive(remote, old_path, new_path)  # both versions: no current record names either
    read = brain_items_by_path_reader(_storage(remote))

    assert read((old_path,)) == ()
    assert read((new_path,)) == ()


def _picked(why_log: WhyLog, record_id: str, path: str) -> None:
    why_log.record_surfacings([Surfacing(
        surfaced_at=NOW, channel="review", item_ref=record_id, record_sha256="x", record_state="open",
        rule="pick_or_park", fields_used={}, record_path=path,
    )])
    why_log.record_response(item_ref=record_id, response="picked_today", response_words="do it", responded_at=NOW)


def _waiting(why_log: WhyLog, record_id: str, path: str) -> None:
    why_log.record_surfacings([Surfacing(
        surfaced_at=NOW, channel="review", item_ref=record_id, record_sha256="waiting-hash",
        record_state="waiting", rule="waiting_on_item", fields_used={}, record_path=path,
    )])


def _moments_context(tmp_path: Path, why_log: WhyLog, reader) -> ReviewContext:
    from test_run import _CapturingService, enabled_settings

    return ReviewContext(
        settings=enabled_settings(str(tmp_path / "why.sqlite3")), why_log=why_log,
        sources=ReviewSources(brain_items=reader), service=_CapturingService(),
    )


def _runs(why_log: WhyLog) -> list[str]:
    return [row["outcome"] for row in why_log._connection.execute("SELECT outcome FROM runs WHERE kind='moments'")]


def test_a_daytime_return_still_brings_back_a_pick_whose_pinned_version_moved_to_the_archive(tmp_path: Path) -> None:
    remote = LANE_B.FakeGitHub()
    old_path = _two_versions(remote)
    _move_to_archive(remote, old_path)
    why_log = WhyLog(tmp_path / "why.sqlite3")
    try:
        _waiting(why_log, OLD_ID, old_path)
        result = run_moments(_moments_context(tmp_path, why_log, brain_items_reader(_storage(remote))), NOW, "pc_on")
        assert (result["outcome"], result["item_ids"]) == ("sent", (NEW_ID,))
        assert "couldnt_read" not in result
        [row] = [row for row in why_log.history() if row.channel == "moment"]
        current_path, current_bytes = _item(NEW_ID, supersedes=(OLD_ID,), title="File the form today")
        from brain_core.records import parse_record
        raw_current = parse_record(current_bytes.decode("utf-8"), current_path)
        assert (row.item_ref, row.record_path, row.record_sha256) == (
            OLD_ID, current_path, raw_current.sha256)
        assert run_moments(_moments_context(tmp_path, why_log, brain_items_reader(_storage(remote))),
                           NOW, "calendar_gap")["outcome"] == "nothing"
    finally:
        why_log.close()


def test_a_daytime_return_that_cannot_read_its_items_says_so_in_the_log_and_the_runs_table(
    tmp_path: Path, caplog,
) -> None:
    remote = LANE_B.FakeGitHub()
    old_path = _two_versions(remote)
    _move_to_archive(remote, old_path)
    # Full current snapshot has two live items, past the unchanged cap of one.
    remote.seed(dict([_item("brn_20260926_030000_cccc3333")]))
    reader = brain_items_reader(_storage(remote), max_records=1)
    why_log = WhyLog(tmp_path / "why.sqlite3")
    try:
        _waiting(why_log, OLD_ID, old_path)
        result = run_moments(_moments_context(tmp_path, why_log, reader), NOW, "pc_on")
        sentence = f"Brain Home holds 2 task records and the hub reads at most 1 at once. {OVER}"
        assert result == {"outcome": "nothing", "signal": "pc_on", "couldnt_read": sentence}
        assert _runs(why_log) == ["pc_on:couldnt_read"]
        assert f"Daytime return (pc_on) couldn't read today's picked and waiting items: {sentence}" in caplog.text
    finally:
        why_log.close()


def test_an_unfinished_pick_in_the_archive_does_not_return_or_read_the_store(tmp_path: Path) -> None:
    remote = LANE_B.FakeGitHub()
    old_path = _two_versions(remote)
    _move_to_archive(remote, old_path)
    why_log = WhyLog(tmp_path / "why.sqlite3")
    try:
        _picked(why_log, OLD_ID, old_path)
        def unused():
            raise AssertionError("an unfinished pick does not trigger a store read")
        result = run_moments(_moments_context(tmp_path, why_log, unused), NOW, "pc_on")
        assert result == {"outcome": "nothing", "signal": "pc_on"}
        assert _runs(why_log) == ["pc_on:nothing"]
        assert not any(row.channel == "moment" for row in why_log.history())
    finally:
        why_log.close()


# --- `gremlin-town brain-home size`: read-only, exit code by level -----------------------------------------


def _size_command(monkeypatch, capsys, remote, *arguments: str) -> tuple[int, str, str]:
    import gremlin_town.entrypoint as entrypoint
    import gremlin_town.runtime as runtime_module

    monkeypatch.setattr(entrypoint, "build_runtime", lambda path, **kwargs: SimpleNamespace(
        brain=SimpleNamespace(), close=lambda: None,
    ))
    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", lambda brain: _storage(remote, cached=False))
    code = entrypoint.main(["brain-home", "size", "--config", "config.toml", *arguments])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_the_size_check_prints_every_scan_and_reads_only_the_tree_and_the_allowance(monkeypatch, capsys) -> None:
    remote = LANE_B.FakeGitHub()
    remote.seed({**_item_files(47), "archive/records/item/2026/09/brn_20260921_050000_aaaaaaaa.md": b"old"})
    remote.events.clear()

    code, out, err = _size_command(monkeypatch, capsys, remote)

    assert (code, err) == (0, "")
    assert out.splitlines()[0] == f"Brain Home at {remote.branch[:12]}: fine"
    assert "  task records: 47 of 5,000 files, 0.0 MB of 32.0 MB (1%) - fine" in out
    assert "  decision records: 0 of 1,000 files (0%) - fine" in out
    assert "  file list: 48 entries of about 26,000 - fine" in out
    assert "Archived: item 1. Receipts: 0. Other files: 0." in out
    assert "GitHub hourly allowance: 4,990 of 5,000 left (10 used)" in out
    assert [event.split()[0] for event in remote.events] == ["GET_REF", "GET_COMMIT", "GET_TREE", "GET_RATE_LIMIT"]


@pytest.mark.parametrize(("files", "code", "level"), [(2500, 1, "heads_up"), (4000, 2, "urgent"), (5001, 2, "over")])
def test_the_size_check_exits_by_level(monkeypatch, capsys, files: int, code: int, level: str) -> None:
    remote = LANE_B.FakeGitHub()
    remote.seed(_item_files(files))

    exit_code, out, _err = _size_command(monkeypatch, capsys, remote, "--format", "json")

    report = json.loads(out)
    assert (exit_code, report["level"]) == (code, level)
    assert report["scans"][0]["name"] == "task records" and report["scans"][0]["files"] == files
    assert report["live_files"] == {"item": files}


def test_the_size_check_says_why_it_could_not_count(monkeypatch, capsys) -> None:
    import gremlin_town.entrypoint as entrypoint
    import gremlin_town.runtime as runtime_module

    class TooBig:
        def record_counts(self):
            raise GitHubStorageError("github_tree_incomplete", "GitHub did not return the complete pinned tree")

    monkeypatch.setattr(entrypoint, "build_runtime", lambda path, **kwargs: SimpleNamespace(
        brain=SimpleNamespace(), close=lambda: None,
    ))
    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", lambda brain: TooBig())
    assert entrypoint.main(["brain-home", "size", "--config", "config.toml"]) == 3
    assert capsys.readouterr().err == f"brain-home: couldn't count Brain Home: {TOO_BIG}\n"

    monkeypatch.setattr(entrypoint, "build_runtime", lambda path, **kwargs: SimpleNamespace(brain=None, close=lambda: None))
    assert entrypoint.main(["brain-home", "size", "--config", "config.toml"]) == 3


def test_the_size_check_never_exits_with_a_level_code_when_it_fails(monkeypatch, capsys, tmp_path: Path) -> None:
    """1 and 2 mean a heads-up and urgent, so a bad config, a missing credential and a usage error each
    have their own code, with the reason on stderr and no traceback."""

    import gremlin_town.entrypoint as entrypoint
    import gremlin_town.runtime as runtime_module

    # A config file that isn't there: the real build_runtime fails.
    assert entrypoint.main(["brain-home", "size", "--config", str(tmp_path / "missing.toml")]) == 3
    assert capsys.readouterr().err.startswith("brain-home: couldn't read the config: ")

    def no_credential(brain):
        raise FileNotFoundError("/run/credentials/github_app_key")

    monkeypatch.setattr(entrypoint, "build_runtime", lambda path, **kwargs: SimpleNamespace(
        brain=SimpleNamespace(), close=lambda: None,
    ))
    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", no_credential)
    assert entrypoint.main(["brain-home", "size", "--config", "config.toml"]) == 3
    assert capsys.readouterr().err == (
        "brain-home: couldn't load the Brain Home credential: FileNotFoundError: /run/credentials/github_app_key\n"
    )

    assert entrypoint.main(["brain-home", "size"]) == 4  # --config missing
    assert entrypoint.main(["brain-home", "count", "--config", "config.toml"]) == 4
    assert entrypoint.main(["brain-home", "size", "--format", "yaml", "--config", "config.toml"]) == 4
    capsys.readouterr()
    assert entrypoint.main(["brain-home", "size", "--help"]) == 0


# --- The Notion view reads within the hub's limits, through a copy ------------------------------------------


def test_a_notion_dry_run_over_600_items_works(tmp_path: Path) -> None:
    from gremlin_town.notion_view.state import ViewState
    from gremlin_town.notion_view.sync import sync_once

    remote = LANE_B.FakeGitHub()
    remote.seed(dict(_item(f"brn_20260921_04{index // 60:02d}{index % 60:02d}_{index:08x}") for index in range(600)))
    state = ViewState(tmp_path / "notion.sqlite3")
    try:
        result = sync_once(
            storage=_storage(remote), client=None, state=state, database_id=None, now=NOW, done_days=7, dry_run=True,
        )
    finally:
        state.close()
    assert (result["rows"], result["create"]) == (600, 600)
