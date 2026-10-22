"""`runtime.py` plugs `brain_source.py`'s readers into `runtime.review_sources` itself,
not only `orientation_context.sources.review` - so `run_review`/`run_moments` (step E)
see real Brain Home items too, now that step E is merged and its `ReviewSources` shape
is stable (f-orientation.md §8's deferred follow-up).

Uses Brain Core's own `FakeGitHub` transport (`tests/unit/test_github_storage.py`,
loaded the same way `tests/mcp/test_brain_composition.py` already does) rather than a
narrow structural fake, so this proves the real `GitHubBrainHomeStorage` read path end
to end, not just this module's own transformation logic (already proven in
`tests/review/test_brain_source.py`).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from brain_core.github_storage import GitHubBrainHomeStorage, GitHubObjectCache

from gremlin_town.runtime import build_runtime
from gremlin_town.transport import FakeTransport
from tests.integration.test_mcp_runtime import mcp_config
from tests.mcp.test_brain_composition import LANE_B, _settings

ITEM_RECORD = b"""---
schema: brain.record/v2
id: brn_20260926_120000_aabb
kind: item
title: Send Sam the deck
state: open
created_at: '2026-09-26T12:00:00+00:00'
updated_at: '2026-09-26T12:00:00+00:00'
sensitivity: private
vaults: []
owners: []
entities: []
evidence:
  - source_type: fixture
    locator: 'fixture:1'
    content_sha256: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'
    excerpt: "I'll send Sam the deck"
supersedes: []
dedupe_key: null
data:
  level: accepted
  size: task
  timing: {}
source:
  said_by: owner
  came_by: direct
  words: "I'll send Sam the deck"
  recorded_by: chatgpt-web
---
"""


def _full_config(tmp_path: Path) -> Path:
    config, _tokens = mcp_config(tmp_path)
    settings = _settings(tmp_path)
    why_log_file = tmp_path / "why.sqlite3"
    with config.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n".join([
                "",
                "[brain_core]",
                "enabled = true",
                f'connector_url = "{settings.connector_url}"',
                f'connector_token_file = "{settings.connector_token_file.as_posix()}"',
                f'github_repository = "{settings.github_repository}"',
                f'github_branch = "{settings.github_branch}"',
                f'github_token_file = "{settings.github_token_file.as_posix()}"',
                f'policy_file = "{settings.policy_file.as_posix()}"',
                f'decision_staging_dir = "{settings.decision_staging_dir.as_posix()}"',
                f'ownership_catalog_file = "{settings.ownership_catalog_file.as_posix()}"',
                f'source_registry_file = "{settings.source_registry_file.as_posix()}"',
                "",
                "[review]",
                "enabled = true",
                'sender_participant = "exosuit-review"',
                'delivery_participant = "hermes-wa"',
                'project = "demo"',
                f'why_log_file = "{why_log_file.as_posix()}"',
                "",
            ])
        )
    return config


def test_review_sources_brain_items_reads_through_the_real_github_storage_path(tmp_path, monkeypatch):
    remote = LANE_B.FakeGitHub()
    remote.seed({"records/item/2026/09/brn_20260926_120000_aabb.md": ITEM_RECORD})
    fake_storage = GitHubBrainHomeStorage(
        repository="trusted/brain-home", branch="accepted", token="fixture-token", transport=remote,
        object_cache=GitHubObjectCache(),
    )

    # Only runtime.py's own storage (review_sources/orientation_context) is faked;
    # runtime.brain_runtime keeps building its real GitHubBrainHomeStorage (unpatched,
    # never called here) - patching the class globally would also break
    # github_brain_provider.py's own isinstance(storage, GitHubBrainHomeStorage) check.
    import gremlin_town.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", lambda brain: fake_storage)
    # With [review] on, the readers go through the hub's copy of Brain Home (finding 6, 30 Sep 2026).
    monkeypatch.setattr(runtime_module, "_cached_brain_storage", lambda brain: (fake_storage, None))

    config = _full_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.review_sources.brain_items is not None
        items = runtime.review_sources.brain_items()
        assert [entry.record.id for entry in items] == ["brn_20260926_120000_aabb"]
        assert items[0].record.title == "Send Sam the deck"

        assert runtime.review_sources.brain_items_by_path is not None
        by_path = runtime.review_sources.brain_items_by_path(
            ("records/item/2026/09/brn_20260926_120000_aabb.md",)
        )
        assert [entry.record.id for entry in by_path] == ["brn_20260926_120000_aabb"]

        # The same storage backs orientation's own sources - one instance, not two.
        assert runtime.orientation_context.sources.review.brain_items is not None
        assert runtime.orientation_context.sources.review.brain_items() == items
    finally:
        runtime.close()


def _answer_fact() -> bytes:
    """Nic's answer to a clarifying question (e-review-and-return.md §3.1): an ordinary v2 fact."""

    from brain_core.records import record_from_mapping, render_record

    words = "DBS, about the card fee"
    record = record_from_mapping({
        "schema": "brain.record/v2", "id": "brn_20260927_010000_ccdd", "kind": "fact",
        "title": "Send Sam the deck", "state": "current",
        "created_at": "2026-09-27T01:00:00+00:00", "updated_at": "2026-09-27T01:00:00+00:00",
        "sensitivity": "private", "vaults": [], "owners": [], "entities": [],
        "evidence": [{"source_type": "fixture", "locator": "fixture:2", "content_sha256": "b" * 64, "excerpt": words}],
        "supersedes": [], "dedupe_key": None,
        "data": {"observed_at": "2026-09-27T01:00:00+00:00", "valid_until": None},
        "body": words,
        "source": {"said_by": "owner", "came_by": "direct", "words": words, "recorded_by": "hermes-wa"},
        "links": [{"type": "about", "target": "brn_20260926_120000_aabb"}],
    })
    return render_record(record).encode("utf-8")


def test_review_sources_brain_facts_reads_facts_through_the_same_storage(tmp_path, monkeypatch):
    remote = LANE_B.FakeGitHub()
    remote.seed({
        "records/item/2026/09/brn_20260926_120000_aabb.md": ITEM_RECORD,
        "records/fact/2026/09/brn_20260927_010000_ccdd.md": _answer_fact(),
    })
    fake_storage = GitHubBrainHomeStorage(
        repository="trusted/brain-home", branch="accepted", token="fixture-token", transport=remote,
        object_cache=GitHubObjectCache(),
    )
    import gremlin_town.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", lambda brain: fake_storage)
    monkeypatch.setattr(runtime_module, "_cached_brain_storage", lambda brain: (fake_storage, None))

    runtime = build_runtime(_full_config(tmp_path), provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.review_sources.brain_facts is not None
        facts = runtime.review_sources.brain_facts()
        assert [(entry.record.id, entry.record.kind) for entry in facts] == [("brn_20260927_010000_ccdd", "fact")]
        # Items stay items: the facts reader is separate, so review sections never see a fact.
        assert [entry.record.kind for entry in runtime.review_sources.brain_items()] == ["item"]
    finally:
        runtime.close()


def test_review_sources_brain_items_is_none_without_brain_core(tmp_path):
    config, _tokens = mcp_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.review_sources.brain_items is None
        assert runtime.review_sources.brain_items_by_path is None
        assert runtime.review_sources.brain_facts is None
    finally:
        runtime.close()


def test_runtime_wires_enabled_mail_path_and_hub_google_token_without_network(tmp_path, monkeypatch):
    import json
    import httpx
    from datetime import UTC, datetime, timedelta
    from types import SimpleNamespace
    import gremlin_town.runtime as runtime_module
    import gremlin_town.sources.google.mail as mail_module

    now = datetime(2026, 10, 1, 22, tzinfo=UTC)
    path = tmp_path / "flagged-mail.json"
    path.write_text(json.dumps({"version": 1, "emails": [dict(message_id="fake1", sender="Fixture sender",
        subject="Fixture subject", score=7, reason="reply today", link="https://mail.google.com/mail/u/0/#all/fake1",
        flagged_at=(now - timedelta(hours=1)).timestamp())]}), encoding="utf-8")
    token = SimpleNamespace(token=lambda: "fixture-hub-token")
    monkeypatch.setattr(runtime_module, "_google_access", lambda google: (SimpleNamespace(writes_calendar=False), token))
    original = mail_module.MailReader
    calls = []
    def labels(request):
        calls.append(request)
        assert request.method == "GET" and request.headers["authorization"] == "Bearer fixture-hub-token"
        assert request.extensions["timeout"]["read"] == 2.0
        return httpx.Response(200, json={"id": "fake1", "labelIds": ["UNREAD"]})
    monkeypatch.setattr(mail_module, "MailReader", lambda google, provider, **kwargs: original(google, provider,
        transport=httpx.MockTransport(labels), **kwargs))
    config = _full_config(tmp_path)
    with config.open("a", encoding="utf-8") as handle:
        handle.write(f'important_mail = true\nimportant_mail_log = "{path.as_posix()}"\n'
            '[google]\nenabled = true\naccount = "owner@example.test"\ncredential_env = "FIXTURE_GOOGLE"\n')
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        reader = runtime.review_sources.important_emails
        assert reader is not None and calls == []  # construction never fetches labels
        assert reader(now, now - timedelta(days=1), ())[0].message_id == "fake1"
        assert len(calls) == 1
    finally:
        runtime.close()


# --- N12 (Ciel slice 2): open notes, the yes-started note's owner, and the handoff notes ------


def _with_handoff_notes(config: Path) -> Path:
    text = config.read_text(encoding="utf-8").replace("[review]\nenabled = true", "[review]\nenabled = true\nhandoff_notes = true")
    assert "handoff_notes = true" in text
    config.write_text(text, encoding="utf-8")
    return config


def test_review_wires_open_notes_the_notes_owner_and_the_handoff_note_keeper(tmp_path, monkeypatch, capsys):
    import json

    import gremlin_town.entrypoint as entrypoint_module
    import gremlin_town.runtime as runtime_module
    from gremlin_town.entrypoint import main as entrypoint_main
    from gremlin_town.review.handoff_notes import HandoffNoteWriter

    from brain_core.continuity import continuity_data
    from brain_core.records import record_from_mapping, render_record

    remote = LANE_B.FakeGitHub()
    fake_storage = GitHubBrainHomeStorage(
        repository="trusted/brain-home", branch="accepted", token="fixture-token", transport=remote,
        object_cache=GitHubObjectCache(),
    )
    # The hub's cached copy of Brain Home, separate here so the test can tell which one a reader uses.
    note = record_from_mapping({
        "schema": "brain.record/v1", "id": "brn_20260928_010000_aabb", "kind": "continuity",
        "title": "Continuity for call-the-bank", "state": "open",
        "created_at": "2026-09-28T01:00:00+00:00", "updated_at": "2026-09-28T01:00:00+00:00",
        "assertion": "explicit", "confidence": 1.0, "sensitivity": "private",
        "vaults": ["vault://brain-home"], "owners": ["brain-home"], "entities": [],
        "evidence": [{"source_type": "fixture", "locator": "fixture:1", "content_sha256": "a" * 64, "excerpt": "x"}],
        "supersedes": [], "dedupe_key": "continuity:call-the-bank",
        "data": continuity_data(
            session_id="call-the-bank", intent="Call the bank", sources=(), next_step="Find the card",
            task_id="brn_20260926_120000_aabb",
        ),
        "body": "",
    })
    cached_remote = LANE_B.FakeGitHub()
    cached_remote.seed({"records/continuity/2026/09/brn_20260928_010000_aabb.md": render_record(note).encode("utf-8")})
    cached_storage = GitHubBrainHomeStorage(
        repository="trusted/brain-home", branch="accepted", token="fixture-token", transport=cached_remote,
        object_cache=GitHubObjectCache(),
    )
    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", lambda brain: fake_storage)
    monkeypatch.setattr(runtime_module, "_cached_brain_storage", lambda brain: (cached_storage, None))

    config = _with_handoff_notes(_full_config(tmp_path))
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert isinstance(runtime.review_handoff_notes, HandoffNoteWriter)
        assert runtime.review_sources.open_notes is not None
        assert [(item.session_id, item.task_id) for item in runtime.review_sources.open_notes()] == [
            ("call-the-bank", "brn_20260926_120000_aabb"),
        ]
        # Orientation's own notes read through the same cached copy while [review] is on.
        assert runtime.orientation_context.sources.open_notes() == {"call-the-bank": "Find the card"}
        # The note a yes starts is stamped with the policy's personal Brain Home owner.
        starter = runtime.orientation_context.continuity
        assert (starter._owner_id, starter._owner_locator) == ("brain-home", "vault://brain-home")
    finally:
        runtime.close()

    # The installed entry point hands the keeper to the poll.
    import gremlin_town.review.poll as poll_module

    real_build = runtime_module.build_runtime
    monkeypatch.setattr(
        entrypoint_module, "build_runtime",
        lambda path, **kwargs: real_build(path, provider=FakeTransport(), **kwargs),
    )
    seen = []
    monkeypatch.setattr(poll_module, "poll_once", lambda context, now: seen.append(context) or ())
    assert entrypoint_main(["review", "moments-poll", "--config", str(config)]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert isinstance(seen[0].handoff_notes, HandoffNoteWriter)
    assert seen[0].settings.handoff_notes is True


def test_handoff_notes_without_brain_core_fails_loudly_at_startup(tmp_path):
    from gremlin_town.runtime import ConfigurationError

    config, _tokens = mcp_config(tmp_path)
    with config.open("a", encoding="utf-8") as handle:
        handle.write(
            "\n[review]\nenabled = true\nhandoff_notes = true\n"
            f'why_log_file = "{(tmp_path / "why.sqlite3").as_posix()}"\n'
        )
    with pytest.raises(ConfigurationError, match="handoff_notes requires"):
        build_runtime(config, provider=FakeTransport(), start_worker=False)


# --- Ciel slice 4: orientation reads Nic's focus projects through the same storage (N4) ---------


def _focus_preference() -> bytes:
    from brain_core.records import record_from_mapping, render_record

    words = "My focus is Exosuit"
    record = record_from_mapping({
        "schema": "brain.record/v2", "id": "brn_20260928_020000_eeff", "kind": "preference",
        "title": "Focus projects", "state": "active",
        "created_at": "2026-09-28T02:00:00+00:00", "updated_at": "2026-09-28T02:00:00+00:00",
        "sensitivity": "private", "vaults": [], "owners": [], "entities": [],
        "evidence": [{"source_type": "fixture", "locator": "fixture:3", "content_sha256": "c" * 64, "excerpt": words}],
        "supersedes": [], "dedupe_key": None,
        "data": {"scope": "personal", "stability": "durable", "evidence": ["mcp-request:fixture"],
                 "correction_history": [], "focus_projects": ["vault_exosuit"]},
        "body": words,
        "source": {"said_by": "owner", "came_by": "direct", "words": words, "recorded_by": "hermes-wa"},
    })
    return render_record(record).encode("utf-8")


def test_orientation_reads_nics_focus_preference_through_the_same_storage(tmp_path, monkeypatch):
    from brain_core.orientation import focus_project_ids

    remote = LANE_B.FakeGitHub()
    remote.seed({
        "records/item/2026/09/brn_20260926_120000_aabb.md": ITEM_RECORD,
        "records/preference/2026/09/brn_20260928_020000_eeff.md": _focus_preference(),
    })
    fake_storage = GitHubBrainHomeStorage(
        repository="trusted/brain-home", branch="accepted", token="fixture-token", transport=remote,
        object_cache=GitHubObjectCache(),
    )
    import gremlin_town.runtime as runtime_module

    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", lambda brain: fake_storage)
    monkeypatch.setattr(runtime_module, "_cached_brain_storage", lambda brain: (fake_storage, None))

    runtime = build_runtime(_full_config(tmp_path), provider=FakeTransport(), start_worker=False)
    try:
        preferences = runtime.orientation_context.sources.preferences()
        assert [record.id for record in preferences] == ["brn_20260928_020000_eeff"]
        assert focus_project_ids(preferences) == frozenset({"vault_exosuit"})
    finally:
        runtime.close()


def test_orientation_has_no_preferences_reader_without_brain_core(tmp_path):
    config, _tokens = mcp_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.orientation_context.sources.preferences is None
        assert runtime.orientation_context.sources.task_sitreps is None
    finally:
        runtime.close()


# --- Ciel slice 8, part 2: orientation reads the hub's task sitreps only while [sitreps] is on (N4) ---------


def _task_sitrep() -> bytes:
    import hashlib
    from datetime import UTC, datetime

    from brain_core import sitreps
    from brain_core.records import Evidence, render_record

    note_sha = hashlib.sha256(b"note").hexdigest()
    record = sitreps.sitrep_record(
        sitreps.start_sitrep(
            project="vault_gtbc_plans", session_id="slice-8", intent="Build slice 8", next_step="Write the tests",
            note_record_id="brn_20260928_030000_aabb", note_content_sha256=note_sha,
            note_updated_at=datetime(2026, 9, 28, 3, 0, tzinfo=UTC),
        ),
        previous=None, now=datetime(2026, 9, 28, 3, 0, 5, tzinfo=UTC),
        evidence=Evidence(source_type="gremlin_town_hub", locator="gt-hub://sitreps/vault_gtbc_plans/slice-8",
                          content_sha256=note_sha, excerpt="Written by the hub.", actor="system:sitreps"),
        owner_id="brain-home", owner_locator="vault://brain-home",
    )
    return f"records/sitrep/2026/09/{record.id}.md", render_record(record).encode("utf-8")


def test_orientation_reads_task_sitreps_only_while_sitreps_are_on(tmp_path, monkeypatch):
    import anyio

    import gremlin_town.runtime as runtime_module

    path, content = _task_sitrep()
    cached_remote = LANE_B.FakeGitHub()
    cached_remote.seed({path: content})
    cached_storage = GitHubBrainHomeStorage(
        repository="trusted/brain-home", branch="accepted", token="fixture-token", transport=cached_remote,
        object_cache=GitHubObjectCache(),
    )
    uncached = GitHubBrainHomeStorage(
        repository="trusted/brain-home", branch="accepted", token="fixture-token", transport=LANE_B.FakeGitHub(),
    )
    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", lambda brain: uncached)
    monkeypatch.setattr(runtime_module, "_cached_brain_storage", lambda brain: (cached_storage, None))

    config = _full_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        # [sitreps] off (the default): no reader, so both asks answer exactly as before.
        assert runtime.sitreps_enabled is False
        assert runtime.orientation_context.sources.task_sitreps is None
    finally:
        runtime.close()

    with config.open("a", encoding="utf-8") as handle:
        handle.write("\n[sitreps]\nenabled = true\n")
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        # On: read through the hub's copy of Brain Home, as the notes are.
        projects = runtime.orientation_context.sources.task_sitreps()
        assert [(project.project, [task.session_id for task in project.tasks]) for project in projects] == [
            ("vault_gtbc_plans", ["slice-8"]),
        ]
        assert projects[0].tasks[0].next_step == "Write the tests"
    finally:
        if runtime.brain_runtime is not None:
            anyio.run(runtime.brain_runtime.aclose)
        runtime.close()


# --- Finding 6 (30 Sep 2026): the review's and placement's readers go through the hub's copy -----


SECOND_ITEM_PATH = "records/item/2026/09/brn_20260926_120000_ccdd.md"
SECOND_ITEM_RECORD = ITEM_RECORD.replace(b"brn_20260926_120000_aabb", b"brn_20260926_120000_ccdd")


def test_with_review_on_items_facts_and_preferences_read_through_the_hubs_copy(tmp_path, monkeypatch):
    """Settling the day took 19.2 s of Diablo's 20 s on 30 Sep: every placement run read Brain Home
    through a storage with no cache. With [review] on, items, facts and preferences now read through
    the hub's copy, the same storage the open notes already used."""

    import gremlin_town.runtime as runtime_module

    plain = GitHubBrainHomeStorage(
        repository="trusted/brain-home", branch="accepted", token="fixture-token", transport=LANE_B.FakeGitHub(),
    )
    copy_remote = LANE_B.FakeGitHub()
    copy_remote.seed({
        "records/item/2026/09/brn_20260926_120000_aabb.md": ITEM_RECORD,
        "records/fact/2026/09/brn_20260927_010000_ccdd.md": _answer_fact(),
        "records/preference/2026/09/brn_20260928_020000_eeff.md": _focus_preference(),
    })
    copy = GitHubBrainHomeStorage(
        repository="trusted/brain-home", branch="accepted", token="fixture-token", transport=copy_remote,
        object_cache=GitHubObjectCache(),
    )
    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", lambda brain: plain)
    monkeypatch.setattr(runtime_module, "_cached_brain_storage", lambda brain: (copy, None))

    runtime = build_runtime(_full_config(tmp_path), provider=FakeTransport(), start_worker=False)
    try:
        sources = runtime.review_sources
        assert [entry.record.id for entry in sources.brain_items()] == ["brn_20260926_120000_aabb"]
        assert [entry.record.id for entry in sources.brain_items_by_path(
            ("records/item/2026/09/brn_20260926_120000_aabb.md",)
        )] == ["brn_20260926_120000_aabb"]
        assert [entry.record.id for entry in sources.brain_facts()] == ["brn_20260927_010000_ccdd"]
        assert [record.id for record in sources.brain_preferences()] == ["brn_20260928_020000_eeff"]
        assert [record.id for record in runtime.orientation_context.sources.preferences()] == [
            "brn_20260928_020000_eeff",
        ]
        # Placement reads the same sources the review does.
        assert runtime.orientation_context.sources.review is sources
    finally:
        runtime.close()


def test_the_hubs_copy_fetches_each_record_file_once_and_still_reads_the_branch(tmp_path, monkeypatch):
    """Through the real hub copy: a second read of an unchanged branch asks GitHub only for the
    branch; a changed branch fetches only the new file; and a fresh process (the 3-minute poll)
    finds the files already in the copy on disk. The branch is read every time, so nothing goes
    stale."""

    import gremlin_town.mcp.brain_assembly as brain_assembly

    remote = LANE_B.FakeGitHub()
    remote.seed({"records/item/2026/09/brn_20260926_120000_aabb.md": ITEM_RECORD})
    monkeypatch.setattr(
        brain_assembly, "_brain_home_credentials", lambda settings, transport, app_client: (LANE_B.TOKEN, remote),
    )

    def requests_made(read) -> list[str]:
        before = len(remote.events)
        read()
        return [event.split()[0] for event in remote.events[before:]]

    config = _full_config(tmp_path)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        brain_items = runtime.review_sources.brain_items
        assert requests_made(brain_items) == ["GET_REF", "GET_COMMIT", "GET_TREE", "GET_BLOB"]
        assert requests_made(brain_items) == ["GET_REF"]
        remote.seed({SECOND_ITEM_PATH: SECOND_ITEM_RECORD})
        assert requests_made(brain_items) == ["GET_REF", "GET_COMMIT", "GET_TREE", "GET_BLOB"]
        assert [entry.record.id for entry in brain_items()] == [
            "brn_20260926_120000_aabb", "brn_20260926_120000_ccdd",
        ]
    finally:
        runtime.close()

    # A fresh process, as the moments poll is every 3 minutes: the files come from the copy on disk.
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert requests_made(runtime.review_sources.brain_items) == ["GET_REF", "GET_COMMIT", "GET_TREE"]
    finally:
        runtime.close()


# --- The storage-limit plan, layer 1 (30 Sep 2026): one limit for every reader, through the copy ---------


class _CopyFile:
    """Stands in for the hub's open copy file (`brain-home-copy.sqlite3`): the hub's limits need it."""

    def close(self) -> None:
        pass


class _RecordingStorage(GitHubBrainHomeStorage):
    """The hub's copy of Brain Home, noting each scan's kinds and limits."""

    def __init__(self, remote) -> None:
        super().__init__(
            repository="trusted/brain-home", branch="accepted", token="fixture-token", transport=remote,
            object_cache=GitHubObjectCache(),
        )
        self.scans: list[tuple[frozenset[str], int, int]] = []

    def snapshot_records(self, **kwargs):
        self.scans.append((kwargs["kinds"], kwargs.get("max_records", 512), kwargs.get("max_bytes", 8 * 1024 * 1024)))
        return super().snapshot_records(**kwargs)


def _open_note() -> bytes:
    from brain_core.continuity import continuity_data
    from brain_core.records import record_from_mapping, render_record

    note = record_from_mapping({
        "schema": "brain.record/v1", "id": "brn_20260928_010000_aabb", "kind": "continuity",
        "title": "Continuity for call-the-bank", "state": "open",
        "created_at": "2026-09-28T01:00:00+00:00", "updated_at": "2026-09-28T01:00:00+00:00",
        "assertion": "explicit", "confidence": 1.0, "sensitivity": "private",
        "vaults": ["vault://brain-home"], "owners": ["brain-home"], "entities": [],
        "evidence": [{"source_type": "fixture", "locator": "fixture:1", "content_sha256": "a" * 64, "excerpt": "x"}],
        "supersedes": [], "dedupe_key": "continuity:call-the-bank",
        "data": continuity_data(
            session_id="call-the-bank", intent="Call the bank", sources=(), next_step="Find the card",
            task_id="brn_20260926_120000_aabb",
        ),
        "body": "",
    })
    return render_record(note).encode("utf-8")


def test_with_review_on_every_reader_and_the_note_starter_scan_through_the_copy_within_the_hubs_limit(
    tmp_path, monkeypatch,
):
    """Every item, fact, preference, note and summary reader, the note a yes starts, and the check behind
    a pinned path read from archive/, scan through the hub's copy within one limit: 5,000 files or 32 MiB
    (docs/product/2026-09-30-brain-home-storage-limit-plan.md, layer 1). The plain storage is never read.
    The 06:00 review also gets its "Brain Home size" reader, from the same copy."""

    import anyio

    import gremlin_town.runtime as runtime_module

    sitrep_path, sitrep = _task_sitrep()
    old_path = "records/item/2026/09/brn_20260926_110000_ccdd.md"
    remote = LANE_B.FakeGitHub()
    remote.seed({
        "records/item/2026/09/brn_20260926_120000_aabb.md": ITEM_RECORD.replace(
            b"supersedes: []", b"supersedes:\n  - brn_20260926_110000_ccdd",
        ),
        "archive/" + old_path: ITEM_RECORD.replace(b"brn_20260926_120000_aabb", b"brn_20260926_110000_ccdd").replace(
            b"2026-09-26T12:00:00", b"2026-09-26T11:00:00",
        ),
        "records/fact/2026/09/brn_20260927_010000_ccdd.md": _answer_fact(),
        "records/preference/2026/09/brn_20260928_020000_eeff.md": _focus_preference(),
        "records/continuity/2026/09/brn_20260928_010000_aabb.md": _open_note(),
        sitrep_path: sitrep,
    })
    copy = _RecordingStorage(remote)
    unread = LANE_B.FakeGitHub()
    plain = GitHubBrainHomeStorage(repository="trusted/brain-home", branch="accepted", token="fixture-token", transport=unread)
    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", lambda brain: plain)
    monkeypatch.setattr(runtime_module, "_cached_brain_storage", lambda brain: (copy, _CopyFile()))
    config = _full_config(tmp_path)
    with config.open("a", encoding="utf-8") as handle:
        handle.write("\n[sitreps]\nenabled = true\n")

    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        review, orientation = runtime.review_sources, runtime.orientation_context
        assert [entry.record.id for entry in review.brain_items()] == ["brn_20260926_120000_aabb"]
        review.brain_facts()
        review.brain_preferences()
        assert [note.session_id for note in review.open_notes()] == ["call-the-bank"]
        review.task_sitreps()
        orientation.sources.open_notes()
        orientation.sources.preferences()
        orientation.sources.task_sitreps()
        # A yes finds the open note already there: nothing is written.
        assert orientation.continuity.start(session_id="call-the-bank", actor="hermes-wa", next_step="").id == (
            "brn_20260928_010000_aabb"
        )
        # A pinned path now in archive/: read from there, kept since the current version names it.
        assert [entry.record.id for entry in review.brain_items_by_path((old_path,))] == ["brn_20260926_110000_ccdd"]
        sizes = review.brain_home_size()
    finally:
        if runtime.brain_runtime is not None:
            anyio.run(runtime.brain_runtime.aclose)
        runtime.close()

    assert {kinds for kinds, _files, _bytes in copy.scans} == {
        frozenset({"item"}), frozenset({"fact"}), frozenset({"preference"}), frozenset({"continuity"}),
        frozenset({"sitrep", "focus_copy"}),
    }
    assert {(files, size) for _kinds, files, size in copy.scans} == {(5000, 32 * 1024 * 1024)}
    assert len([kinds for kinds, _files, _bytes in copy.scans if kinds == frozenset({"continuity"})]) == 3
    assert unread.events == []
    assert [entry.name for entry in sizes][:1] == ["task records"] and all(entry.level == "ok" for entry in sizes)


def test_without_the_copy_file_every_reader_keeps_the_512_file_default(tmp_path, monkeypatch, caplog):
    """The review, the moments poll and placement are fresh processes every few minutes: with the copy
    in memory only, each would download every file each time, so 5,000 files could spend GitHub's hourly
    allowance. With the copy file missing, every reader and the hub's note keeper keep Brain Core's
    defaults, 512 files and 8 MiB, and the journal says so (review of the branch, 30 Sep 2026)."""

    import gremlin_town.runtime as runtime_module

    remote = LANE_B.FakeGitHub()
    remote.seed({
        "records/item/2026/09/brn_20260926_120000_aabb.md": ITEM_RECORD,
        "records/continuity/2026/09/brn_20260928_010000_aabb.md": _open_note(),
    })
    memory_only = _RecordingStorage(remote)
    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", lambda brain: memory_only)
    monkeypatch.setattr(runtime_module, "_cached_brain_storage", lambda brain: (memory_only, None))

    runtime = build_runtime(_with_handoff_notes(_full_config(tmp_path)), provider=FakeTransport(), start_worker=False)
    try:
        runtime.review_sources.brain_items()
        runtime.review_sources.brain_facts()
        runtime.review_sources.open_notes()
        runtime.orientation_context.sources.preferences()
        writer = runtime.review_handoff_notes
    finally:
        runtime.close()

    assert {(files, size) for _kinds, files, size in memory_only.scans} == {(512, 8 * 1024 * 1024)}
    assert (writer._max_records, writer._max_bytes) == (512, 8 * 1024 * 1024)
    assert "Brain Home copy file unavailable: Brain Home readers keep the 512-file limit" in caplog.text


def test_with_review_off_the_plain_storage_keeps_the_512_file_default(tmp_path, monkeypatch):
    import gremlin_town.runtime as runtime_module

    remote = LANE_B.FakeGitHub()
    remote.seed({"records/item/2026/09/brn_20260926_120000_aabb.md": ITEM_RECORD})
    plain = _RecordingStorage(remote)
    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", lambda brain: plain)

    config = _full_config(tmp_path)
    text = config.read_text(encoding="utf-8").replace("[review]\nenabled = true", "[review]\nenabled = false")
    assert "enabled = false" in text
    config.write_text(text, encoding="utf-8")
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        assert runtime.review_blob_store is None
        assert [entry.record.id for entry in runtime.review_sources.brain_items()] == ["brn_20260926_120000_aabb"]
    finally:
        runtime.close()

    assert plain.scans == [(frozenset({"item"}), 512, 8 * 1024 * 1024)]


@pytest.mark.parametrize(("copy_file", "line"), [
    (None, (
        "your task records are at 256 of the 512 the hub reads at once. Nothing is lost. Past 512, the review, "
        "placement, \"where am I?\" and \"what next?\" stop seeing your tasks; ask for the storage tidy-up (old "
        "versions moved into Brain Home's archive) before then."
    )),
    (_CopyFile(), None),
])
def test_the_size_line_measures_against_the_limit_in_force(tmp_path, monkeypatch, copy_file, line):
    """Without the copy file the readers stop at 512, so the 06:00 review warns against 512; with it, 256
    task files are far from 5,000 and nothing is said."""

    import gremlin_town.runtime as runtime_module
    from gremlin_town.review.collect import brain_home_size_line

    remote = LANE_B.FakeGitHub()
    remote.seed({f"records/item/2026/09/brn_20260921_040000_{index:08x}.md": b"x" for index in range(256)})
    storage = _RecordingStorage(remote)
    monkeypatch.setattr(runtime_module, "_orientation_brain_storage", lambda brain: storage)
    monkeypatch.setattr(runtime_module, "_cached_brain_storage", lambda brain: (storage, copy_file))

    runtime = build_runtime(_full_config(tmp_path), provider=FakeTransport(), start_worker=False)
    try:
        assert brain_home_size_line(runtime.review_sources.brain_home_size, {}) == line
    finally:
        runtime.close()
