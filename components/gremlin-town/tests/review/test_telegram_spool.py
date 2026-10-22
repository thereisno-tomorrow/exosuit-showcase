"""`[review] telegram_spool_dir`: each Telegram message is written for the courier (step E §2).

Diablo's Hermes lets no agent send on its own, so `deploy/telegram-courier.sh` posts what GT
writes here. Unset (the default), nothing is written anywhere. Set, the text is exactly the
GT request's packet context, written after the request exists and never twice. The review
and moments paths run against a real `GremlinTownService`, as in `test_run.py`.
"""

from __future__ import annotations

import os
import stat
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from brain_core.records import RECORD_SCHEMA_V2, Record
from brain_core.review import ItemRecord

from gremlin_town.contracts import Actor, CreateRequest
from gremlin_town.domain import Participant, Policy
from gremlin_town.review import spool as spool_module
from gremlin_town.review.collect import ReviewSources
from gremlin_town.review.run import ReviewContext, run_moments, run_review
from gremlin_town.review.settings import ReviewSettings, ReviewSettingsError
from gremlin_town.review.spool import SPOOL_FILE_MODE, SpoolError, write_message
from gremlin_town.review.why_log import Surfacing, WhyLog
from gremlin_town.service import GremlinTownService
from gremlin_town.storage import SQLiteStore

NOW = datetime(2026, 9, 28, 22, 0, tzinfo=timezone.utc)  # 06:00 SGT on 2026-09-29
REVIEW_FILE = "review-2026-09-29.txt"


def test_was_sent_uses_only_a_safe_regular_file_in_sent(tmp_path):
    from gremlin_town.review.spool import was_sent
    folder = _spool(tmp_path)
    write_message(str(folder), "moment-test", "Return")
    assert not was_sent(str(folder), "moment-test")
    sent = folder / "sent"
    sent.mkdir()
    (sent / "ledger.tsv").write_text("moment-test\tsent")
    assert not was_sent(str(folder), "moment-test")
    (folder / "moment-test.txt").rename(sent / "moment-test.txt")
    assert was_sent(str(folder), "moment-test")
    with pytest.raises(SpoolError):
        was_sent(str(folder), "../moment-test")


def _settings(tmp_path: Path, spool: Path | None) -> ReviewSettings:
    table = {
        "enabled": True,
        "sender_participant": "exosuit-review",
        "delivery_participant": "hermes-wa",
        "project": "vault_home",
        "why_log_file": str(tmp_path / "why.sqlite3"),
    }
    if spool is not None:
        table["telegram_spool_dir"] = str(spool)
    return ReviewSettings.from_table(table)


COURIER_NEXT_ACTION = 'Nothing to post: the courier posts the text. Acknowledge, then return the result "posted by the courier"'


def _item(record_id: str, title: str, timing: dict | None = None) -> ItemRecord:
    # A sorted task (PRD §4.1: it has a block type), so "To sort" stays out of these tests.
    data: dict = {"level": "accepted", "size": "task", "owed_to": None, "repeat": None, "timing": timing or {},
                  "block": "admin"}
    record = Record(
        schema=RECORD_SCHEMA_V2, id=record_id, kind="item", title=title, state="open",
        created_at=NOW, updated_at=NOW, assertion=None, confidence=None, sensitivity="private",
        vaults=(), owners=(), entities=(), evidence=(), supersedes=(), dedupe_key=None,
        data=data, body="", source=None, links=(),
    )
    return ItemRecord(record=record, path=f"records/item/2026/09/{record_id}.md")


class _Recording:
    """The real service, recording each request and what the spool held when it was made."""

    def __init__(self, service: GremlinTownService, spool: Path | None) -> None:
        self.service = service
        self.spool = spool
        self.commands: list[CreateRequest] = []
        self.spooled_before: list[list[str]] = []

    def requests_create(self, actor: Actor, command: CreateRequest):
        self.spooled_before.append(sorted(p.name for p in self.spool.iterdir()) if self.spool else [])
        receipt = self.service.requests_create(actor, command)
        self.commands.append(command)
        return receipt

    def __getattr__(self, name: str):
        return getattr(self.service, name)


@pytest.fixture
def service(tmp_path: Path) -> GremlinTownService:
    policy = Policy(
        participants=(
            Participant(
                "exosuit-review", "Exosuit review", "hub@example.test",
                frozenset({"vault_home"}), frozenset({"private"}), read_all_requests=True,
            ),
            Participant("hermes-wa", "Diablo", "hub@example.test", frozenset({"vault_home"}), frozenset({"private"})),
            Participant("local-codex", "Codex", "hub@example.test", frozenset({"vault_home"}), frozenset({"private"})),
        ),
        routes=frozenset({
            ("vault_home", "exosuit-review", "hermes-wa", "private"),
            ("vault_home", "exosuit-review", "local-codex", "private"),
        }),
    )
    return GremlinTownService(SQLiteStore(tmp_path / "gremlin.db", now=lambda: NOW), policy, now=lambda: NOW)


def _review_context(tmp_path: Path, service, spool: Path | None) -> tuple[ReviewContext, WhyLog]:
    why_log = WhyLog(tmp_path / "why.sqlite3")
    # Explicit synthetic empty pre-move INPUT for these fake post-move captures.
    why_log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
    due = _item("brn_invoice", "Send the invoice — Acme", {"due": "2026-09-29"})
    context = ReviewContext(
        settings=_settings(tmp_path, spool),
        why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (due,)),
        service=service,
    )
    return context, why_log


def _spool(tmp_path: Path) -> Path:
    folder = tmp_path / "spool"
    folder.mkdir()
    return folder


def test_the_setting_is_off_by_default_and_must_be_a_non_empty_string() -> None:
    assert ReviewSettings.from_table(None).telegram_spool_dir is None
    assert ReviewSettings.from_table({"enabled": False}).telegram_spool_dir is None
    configured = ReviewSettings.from_table({"telegram_spool_dir": "/var/lib/gremlin-town-brain/why/spool"})
    assert configured.telegram_spool_dir == "/var/lib/gremlin-town-brain/why/spool"
    for bad in ("", 7, True):
        with pytest.raises(ReviewSettingsError):
            ReviewSettings.from_table({"telegram_spool_dir": bad})


def test_unset_writes_no_file_and_keeps_the_request_wording(tmp_path: Path, service: GremlinTownService) -> None:
    folder = _spool(tmp_path)  # exists, but is not configured
    recording = _Recording(service, None)
    context, why_log = _review_context(tmp_path, recording, None)
    assert run_review(context, NOW)["outcome"] == "sent"
    assert list(folder.iterdir()) == []
    assert not list(tmp_path.rglob("*.txt"))
    (command,) = recording.commands
    assert "four parts on start" in command.packet.objective
    assert "Post constraints.short once" in command.packet.next_action
    assert "gremlin-town-review" in command.packet.next_action
    assert "## Look" in command.packet.context
    why_log.close()


def test_set_writes_the_request_text_exactly_after_the_request_exists(
    tmp_path: Path, service: GremlinTownService
) -> None:
    folder = _spool(tmp_path)
    recording = _Recording(service, folder)
    context, why_log = _review_context(tmp_path, recording, folder)

    result = run_review(context, NOW)

    assert result["outcome"] == "sent"
    assert recording.spooled_before == [[]]  # nothing was in the spool when the request was made
    (command,) = recording.commands
    assert command.client_operation_id == "review-2026-09-29"
    written = folder / REVIEW_FILE
    assert written.read_bytes() == command.packet.constraints["short"].encode("utf-8")
    assert written.read_bytes() != command.packet.context.encode("utf-8")
    assert "Send the invoice — Acme" in written.read_text(encoding="utf-8")
    assert "## Catch up" in command.packet.context and "## New" in command.packet.context
    assert sorted(p.name for p in folder.iterdir()) == [REVIEW_FILE]
    assert "courier posts only the short message" in command.packet.next_action
    assert "post no parts on wake" in command.packet.next_action
    assert "gremlin-town-review" in command.packet.next_action
    why_log.close()


def test_clarifying_questions_wait_for_nics_answer_and_stay_out_of_the_spooled_text(
    tmp_path: Path, service: GremlinTownService
) -> None:
    folder = _spool(tmp_path)
    recording = _Recording(service, folder)
    why_log = WhyLog(tmp_path / "why.sqlite3")
    # Explicit synthetic empty pre-move INPUT for this fake post-move capture.
    why_log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
    context = ReviewContext(
        settings=_settings(tmp_path, folder),
        why_log=why_log,
        sources=ReviewSources(
            brain_items=lambda: (_item("brn_bank", "Call the bank"),),
            brain_facts=lambda: (),
            calendar_events=lambda start, end: (),  # an empty calendar and no open requests,
            open_requests=lambda: (),  # so the review has no "Couldn't see" lines
        ),
        service=recording,
    )

    assert run_review(context, NOW)["clarify_count"] == 0
    (command,) = recording.commands
    assert "clarify" not in command.packet.constraints
    assert "gremlin-town-review" in command.packet.next_action
    assert (folder / REVIEW_FILE).read_text(encoding="utf-8") == command.packet.constraints["short"]
    assert "## New" in command.packet.context and "Call the bank" in command.packet.context
    assert "## New" not in (folder / REVIEW_FILE).read_text(encoding="utf-8")
    assert "What's the first step" not in command.packet.context
    why_log.close()


def test_a_same_day_replay_writes_nothing_more(tmp_path: Path, service: GremlinTownService) -> None:
    folder = _spool(tmp_path)
    context, why_log = _review_context(tmp_path, service, folder)
    run_review(context, NOW)
    before = (folder / REVIEW_FILE).read_bytes()

    assert run_review(context, NOW) == {"outcome": "already_sent"}
    assert sorted(p.name for p in folder.iterdir()) == [REVIEW_FILE]
    assert (folder / REVIEW_FILE).read_bytes() == before
    why_log.close()


def test_a_replayed_write_never_overwrites_or_resends(tmp_path: Path) -> None:
    folder = _spool(tmp_path)
    first = write_message(str(folder), "moment-pc_on-hermes-wa-pc_on-3", "1. File the form")
    assert first == folder / "moment-pc_on-hermes-wa-pc_on-3.txt"

    # Still waiting for the courier: the file is left exactly as it was.
    assert write_message(str(folder), "moment-pc_on-hermes-wa-pc_on-3", "something else") is None
    assert first.read_text(encoding="utf-8") == "1. File the form"

    # Already posted (the courier moved it to sent/): not spooled again.
    (folder / "sent").mkdir()
    first.replace(folder / "sent" / first.name)
    assert write_message(str(folder), "moment-pc_on-hermes-wa-pc_on-3", "1. File the form") is None
    assert not first.exists()
    assert sorted(p.name for p in folder.iterdir()) == ["sent"]


def test_the_file_is_mode_0640(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    folder = _spool(tmp_path)
    modes: list[int] = []
    real_chmod = os.chmod

    def recording_chmod(path, mode, *args, **kwargs):
        modes.append(mode)
        return real_chmod(path, mode, *args, **kwargs)

    monkeypatch.setattr(spool_module.os, "chmod", recording_chmod)
    written = write_message(str(folder), "review-2026-09-29", "Nothing due, nothing new. Calendar is clear.")
    assert modes == [SPOOL_FILE_MODE] and SPOOL_FILE_MODE == 0o640
    if os.name == "posix":  # Windows keeps only a read-only bit
        assert stat.S_IMODE(written.stat().st_mode) == 0o640


def test_moments_spool_damiens_copy_only(tmp_path: Path, service: GremlinTownService) -> None:
    folder = _spool(tmp_path)
    recording = _Recording(service, folder)
    why_log = WhyLog(tmp_path / "why.sqlite3")
    item = _item("brn_admin", "File the form")
    why_log.record_surfacings([
        Surfacing(
            surfaced_at=NOW, channel="review", item_ref="brn_admin", record_sha256=item.sha256,
            record_state="waiting", rule="waiting_on_item", fields_used={}, record_path=item.path,
        )
    ])
    context = ReviewContext(
        settings=_settings(tmp_path, folder),
        why_log=why_log,
        sources=ReviewSources(brain_items=lambda: (item,)),
        service=recording,
    )

    result = run_moments(context, NOW, "pc_on", also_notify="local-codex", signal_id="pc_on-7")

    assert result["outcome"] == "sent"
    assert {entry["recipient"] for entry in result["deliveries"]} == {"hermes-wa", "local-codex"}
    to_damien = next(c for c in recording.commands if c.recipient == "hermes-wa")
    assert to_damien.client_operation_id == "moment-pc_on-hermes-wa-pc_on-7"
    assert sorted(p.name for p in folder.iterdir()) == ["moment-pc_on-hermes-wa-pc_on-7.txt"]
    assert (folder / "moment-pc_on-hermes-wa-pc_on-7.txt").read_bytes() == to_damien.packet.context.encode("utf-8")
    assert recording.spooled_before == [[], []]  # written after both requests
    assert to_damien.packet.objective == "This update; the courier posts it to Nic's Telegram"
    assert (to_damien.packet.next_action, to_damien.packet.expected_reply) == (COURIER_NEXT_ACTION, "posted by the courier")
    to_codex = next(c for c in recording.commands if c.recipient == "local-codex")
    assert to_codex.packet.objective == "Show Nic this update now, since he is using you (D24)"  # unchanged
    assert to_codex.packet.context == to_damien.packet.context
    why_log.close()


def test_unfinished_pick_creates_no_courier_file_or_agent_delivery(tmp_path: Path, service: GremlinTownService) -> None:
    folder = _spool(tmp_path)
    recording = _Recording(service, folder)
    why_log = WhyLog(tmp_path / "why.sqlite3")
    item = _item("brn_unfinished", "File the form")
    why_log.record_surfacings([Surfacing(
        surfaced_at=NOW, channel="review", item_ref=item.record.id, record_sha256=item.sha256,
        record_state="open", rule="pick_or_park", fields_used={}, record_path=item.path,
    )])
    why_log.record_response(item_ref=item.record.id, response="picked_today", response_words="do it", responded_at=NOW)
    context = ReviewContext(_settings(tmp_path, folder), why_log, ReviewSources(brain_items=lambda: (item,)), recording)
    assert run_moments(context, NOW, "pc_on", also_notify="local-codex", signal_id="pc_on-negative") == {
        "outcome": "nothing", "signal": "pc_on"}
    assert recording.commands == [] and list(folder.iterdir()) == []
    assert not any(row.channel == "moment" for row in why_log.history())
    why_log.close()


def test_a_missing_folder_fails_before_anything_is_logged_or_sent(
    tmp_path: Path, service: GremlinTownService
) -> None:
    recording = _Recording(service, None)
    context, why_log = _review_context(tmp_path, recording, tmp_path / "no-such-spool")

    with pytest.raises(SpoolError, match="does not exist"):
        run_review(context, NOW)
    with pytest.raises(SpoolError, match="does not exist"):
        run_moments(context, NOW, "pc_on", signal_id="pc_on-1")

    assert recording.commands == []
    assert why_log.history() == ()
    assert why_log.sent_today("review", date(2026, 9, 29)) is False
    why_log.close()


def test_a_write_failure_after_the_request_is_loud_and_the_request_stays(
    tmp_path: Path, service: GremlinTownService, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = _spool(tmp_path)
    recording = _Recording(service, folder)
    context, why_log = _review_context(tmp_path, recording, folder)

    def full_disk(source, target):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(spool_module.os, "link", full_disk)
    with pytest.raises(SpoolError, match=REVIEW_FILE):
        run_review(context, NOW)

    # The request to Diablo exists (he reads it when Nic replies) and the run is recorded, so a
    # rerun never rebuilds a different review under the same client_operation_id.
    assert [c.client_operation_id for c in recording.commands] == ["review-2026-09-29"]
    assert run_review(context, NOW) == {"outcome": "already_sent"}
    assert list(folder.iterdir()) == []  # the temporary file was removed
    why_log.close()


@pytest.mark.parametrize("name", ["../escape", "sub/dir", ".hidden", "", "a b"])
def test_an_unsafe_name_is_refused(tmp_path: Path, name: str) -> None:
    folder = _spool(tmp_path)
    with pytest.raises(SpoolError, match="not a safe spool file name"):
        write_message(str(folder), name, "text")
    assert list(folder.iterdir()) == []
