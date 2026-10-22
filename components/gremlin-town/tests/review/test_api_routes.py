"""`/v1/moments/signals` and `/v1/review/responses`: absent (404) when [review]
is disabled; caller-stamped and why-log-backed when enabled (step E spec §8)."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace

from gremlin_town.api.review import record_moments_signal, record_review_response
from gremlin_town.mcp.brain_runtime import BrainRuntimeError
from gremlin_town.review.why_log import Surfacing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gremlin_town.runtime import build_runtime
from gremlin_town.transport import FakeTransport


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


TOKENS = {
    "local-codex": "codex-token-with-more-than-thirty-two-bytes-00",
    "hermes-wa": "damien-token-with-more-than-thirty-two-bytes-00",
    "exosuit-review": "review-token-with-more-than-thirty-two-bytes-0",
    "fleet-boss-grok": "fleet-fixture-token-with-more-than-thirty-two-bytes",
}


def _headers(participant: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKENS[participant]}"}


def write_config(root: Path, *, review_enabled: bool) -> Path:
    participants = root / "participants.toml"
    entries = []
    for participant_id in TOKENS:
        entries += [
            "[[participant]]",
            f'id = "{participant_id}"',
            f'display_name = "{participant_id}"',
            'address = "hub@example.test"',
            'role = "participant"',
            "enabled = true",
            f'token_sha256 = "{_hash(TOKENS[participant_id])}"',
            'projects = ["vault_home"]',
            'sensitivities = ["private"]',
            "",
        ]
    entries += [
        "[[route]]",
        'project = "vault_home"',
        'source = "exosuit-review"',
        'destination = "hermes-wa"',
        'sensitivity = "private"',
    ]
    participants.write_text("\n".join(entries), encoding="utf-8")

    config_lines = [
        "[server]",
        f'database = "{(root / "gremlin.sqlite3").as_posix()}"',
        "[participants]",
        f'file = "{participants.as_posix()}"',
        "[recovery]",
        "normal_clients_allowed = true",
        "outbound_dispatch_allowed = false",
    ]
    if review_enabled:
        why_log_file = root / "why.sqlite3"
        config_lines += [
            "[review]",
            "enabled = true",
            'sender_participant = "exosuit-review"',
            'delivery_participant = "hermes-wa"',
            'project = "vault_home"',
            f'why_log_file = "{why_log_file.as_posix()}"',
        ]
    (root / "config.toml").write_text("\n".join(config_lines), encoding="utf-8")
    return root / "config.toml"


def test_routes_absent_when_review_disabled(tmp_path: Path) -> None:
    config = write_config(tmp_path, review_enabled=False)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        with TestClient(runtime.app) as client:
            signal = client.post("/v1/moments/signals", headers=_headers("local-codex"), json={"kind": "pc_on"})
            response = client.post(
                "/v1/review/responses", headers=_headers("hermes-wa"),
                json={"item_ref": "brn_x", "response": "picked_today"},
            )
        assert signal.status_code == 404
        assert response.status_code == 404
    finally:
        runtime.close()


def test_signal_is_caller_stamped_and_recorded(tmp_path: Path) -> None:
    config = write_config(tmp_path, review_enabled=True)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        with TestClient(runtime.app) as client:
            response = client.post("/v1/moments/signals", headers=_headers("local-codex"), json={"kind": "pc_on"})
            assert response.status_code == 200
            body = response.json()
            assert body == {"recorded": True, "kind": "pc_on", "actor": "local-codex", "at": body["at"]}
            rows = runtime.review_why_log._connection.execute("SELECT * FROM signals").fetchall()
            assert len(rows) == 1
            assert rows[0]["actor"] == "local-codex"
            assert rows[0]["kind"] == "pc_on"
    finally:
        runtime.close()


def test_signal_rejects_unknown_kind(tmp_path: Path) -> None:
    config = write_config(tmp_path, review_enabled=True)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        with TestClient(runtime.app) as client:
            response = client.post(
                "/v1/moments/signals", headers=_headers("local-codex"), json={"kind": "phone_unlock"}
            )
        assert response.status_code == 422
    finally:
        runtime.close()


def test_review_response_updates_an_existing_surfacing(tmp_path: Path) -> None:
    config = write_config(tmp_path, review_enabled=True)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        from datetime import UTC, datetime

        from gremlin_town.review.why_log import Surfacing

        runtime.review_why_log.record_surfacings([
            Surfacing(
                surfaced_at=datetime(2026, 9, 26, 22, 0, tzinfo=UTC),
                channel="review", item_ref="brn_x", record_sha256="a" * 64,
                record_state="open", rule="pick_or_park", fields_used={},
            )
        ])
        with TestClient(runtime.app) as client:
            response = client.post(
                "/v1/review/responses", headers=_headers("hermes-wa"),
                json={"item_ref": "brn_x", "response": "picked_today", "response_words": "do it"},
            )
            assert response.status_code == 200
            assert response.json()["relayed_by"] == "hermes-wa"
            history = runtime.review_why_log.history()
            assert history[0].response == "picked_today"
    finally:
        runtime.close()


def test_review_response_for_unknown_item_is_404(tmp_path: Path) -> None:
    config = write_config(tmp_path, review_enabled=True)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        with TestClient(runtime.app) as client:
            response = client.post(
                "/v1/review/responses", headers=_headers("hermes-wa"),
                json={"item_ref": "brn_missing", "response": "picked_today"},
            )
        assert response.status_code == 404
    finally:
        runtime.close()


def test_review_response_rejects_unknown_response_value(tmp_path: Path) -> None:
    config = write_config(tmp_path, review_enabled=True)
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    try:
        with TestClient(runtime.app) as client:
            response = client.post(
                "/v1/review/responses", headers=_headers("hermes-wa"),
                json={"item_ref": "brn_x", "response": "maybe"},
            )
        assert response.status_code == 422
    finally:
        runtime.close()


def test_marker_review_and_moments_validate_without_why_log_writes(tmp_path):
    runtime = build_runtime(write_config(tmp_path, review_enabled=True), provider=FakeTransport(), start_worker=False)
    marker = tmp_path / "work-replay-check-only"
    marker.write_text("fixture replay")
    async def close():
        pass
    runtime.brain_runtime = SimpleNamespace(_memory=SimpleNamespace(replay_marker=marker), aclose=close)
    runtime.review_why_log.record_surfacings([Surfacing(datetime.now(UTC), "review", "brn_first", "a" * 64,
        "question", {}, question="Is that your day?", offered=("accept",))])
    with TestClient(runtime.app) as client:
        for kind in ("day_settled", "pc_on"):
            result = client.post("/v1/moments/signals", headers=_headers("hermes-wa"), json={"kind": kind})
            assert result.status_code == 200 and result.json()["status"] == "validated"
        result = client.post("/v1/review/responses", headers=_headers("hermes-wa"),
            json={"item_ref": "brn_first", "response": "accepted", "response_words": "yes"})
        assert result.status_code == 200 and result.json()["status"] == "validated"
        assert not runtime.review_why_log.signals_since(0)
        assert runtime.review_why_log.history()[0].response is None
        for path, body in (("/v1/moments/signals", {"kind": "day_settled"}),
                           ("/v1/review/responses", {"item_ref": "brn_first", "response": "accepted"})):
            result = client.post(path, headers=_headers("local-codex"), json=body)
            assert result.status_code == 403 and result.json()["error"]["fix"]
        assert client.post("/v1/moments/signals", headers=_headers("local-codex"),
            json={"kind": "pc_on"}).status_code == 200

def test_unknown_code_relayer_is_refused_by_functions(tmp_path):
    runtime = build_runtime(write_config(tmp_path, review_enabled=True), provider=FakeTransport(), start_worker=False)
    for function, payload in ((record_moments_signal, {"kind": "day_settled", "detail": None}),
                              (record_review_response, {"item_ref": "none", "response": "accepted", "response_words": "yes"})):
        with pytest.raises(BrainRuntimeError) as error:
            function(runtime.review_why_log, caller="hermes-wa", relayer="system:unknown",
                now=datetime.now(UTC), **payload)
        assert error.value.code == "ACCESS_DENIED" and error.value.fix
    runtime.review_why_log.close()


def test_host_registered_relayer_uses_the_same_review_and_day_answer_gate(tmp_path):
    runtime = build_runtime(write_config(tmp_path, review_enabled=True), provider=FakeTransport(), start_worker=False)
    log = runtime.review_why_log
    now = datetime.now(UTC)
    caller = "system:fixture-relayer"
    try:
        log.record_surfacings([Surfacing(now, "review", "brn_fixture", "", "question", {},
                                        question="Pick?", offered=("picked_today",))])
        reference = log.review_references(now)[0]
        result = record_review_response(log, caller="local-codex", relayer=caller, code_callers=frozenset({caller}),
            item_refs=[reference], response="picked_today", response_words="yes", now=now)
        assert result["recorded"] and result["relayed_by"] == caller
        day = record_moments_signal(log, caller="local-codex", relayer=caller, code_callers=frozenset({caller}),
                                   kind="day_settled", detail="yes", now=now)
        assert day["actor"] == caller and day["recorded"]
        with TestClient(runtime.app) as client:
            denied = client.post("/v1/review/responses", headers=_headers("local-codex"),
                json={"item_refs": [reference], "response": "picked_today", "relayer": caller, "code_callers": [caller]})
            assert denied.status_code == 403
    finally:
        log.close()


def test_current_day_flag_is_door_only_and_logs_before_the_reply(tmp_path, monkeypatch):
    from brain_core.review import ItemRecord
    from gremlin_town.review.collect import ReviewSources
    from gremlin_town.review.run import ReviewContext, run_review
    from test_run import make_item
    runtime = build_runtime(write_config(tmp_path, review_enabled=True), provider=FakeTransport(), start_worker=False)
    log = runtime.review_why_log
    # The sent 06:00 packet and its current-part read must be on the same personal
    # day even when this test runs before 04:00 in Singapore.
    now = datetime(2026, 10, 4, 1, tzinfo=UTC)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.replace(tzinfo=None)
    monkeypatch.setattr("gremlin_town.api.review.datetime", Clock)
    runtime.review_sources = ReviewSources(brain_items=lambda: (ItemRecord(make_item(id="brn_fixture", title="Fixture task"), "fixture.md"),))
    log.stage_morning_bootstrap(input_sha="a" * 40, first_ids=())
    class Sender:
        def requests_create(self, actor, command):
            return SimpleNamespace(request_id="req_fixture")
    run_review(ReviewContext(runtime.review, log, runtime.review_sources, Sender()), now)
    with TestClient(runtime.app) as client:
        before = len(log.history())
        refused = client.post("/v1/review/responses", headers=_headers("local-codex"), json={"current_day": True})
        assert refused.status_code == 403 and len(log.history()) == before
        answer = client.post("/v1/review/responses", headers=_headers("hermes-wa"), json={"current_day": True})
        assert answer.status_code == 200, answer.json()
        for line in answer.json()["current_day"]["lines"]:
            assert log.answer_context(line["reference"]).question == line["text"]
        invalid = client.post("/v1/review/responses", headers=_headers("hermes-wa"), json={"current_day": "true"})
        assert invalid.status_code == 422
        def failed():
            raise RuntimeError("fixture read failed")
        runtime.review_sources = ReviewSources(brain_items=failed)
        failed_reply = client.post("/v1/review/responses", headers=_headers("hermes-wa"), json={"current_day": True})
        assert failed_reply.status_code == 500 and "unavailable" in failed_reply.json()["error"]["message"]


def test_review_plural_references_answer_today_lines_and_keep_other_channels(tmp_path):
    runtime = build_runtime(write_config(tmp_path, review_enabled=True), provider=FakeTransport(), start_worker=False)
    log = runtime.review_why_log
    now = datetime.now(UTC)
    log.record_surfacings([
        Surfacing(now, "review", ref, "raw", "needs_you", {}, question=f"Keep {ref}?", offered=("keep",))
        for ref in ("brn_first", "brn_second")
    ] + [Surfacing(now, "weekly", "brn_first", "raw", "question", {}, question="Weekly?")])
    refs = list(log.review_references(now))
    try:
        with TestClient(runtime.app) as client:
            rejected = client.post("/v1/review/responses", headers=_headers("local-codex"),
                                   json={"item_refs": refs, "response": "answered"})
            assert rejected.status_code == 403
            assert all(row.response is None for row in log.history())
            answer = client.post("/v1/review/responses", headers=_headers("hermes-wa"),
                                 json={"item_refs": refs, "response": "answered", "response_words": "yes"})
            assert answer.status_code == 200
            assert answer.json()["item_refs"] == ["brn_first", "brn_second"]
            assert [row.response for row in log.history() if row.channel == "review"] == ["answered", "answered"]
            assert [row.response for row in log.history() if row.channel == "weekly"] == [None]
    finally:
        runtime.close()


def test_review_plural_batch_validates_every_reference_before_writing(tmp_path):
    runtime = build_runtime(write_config(tmp_path, review_enabled=True), provider=FakeTransport(), start_worker=False)
    log = runtime.review_why_log
    log.record_surfacings([Surfacing(datetime.now(UTC), "review", "brn_first", "raw", "your_day", {},
                                    question="Pick first?", offered=("picked_today",))])
    try:
        with TestClient(runtime.app) as client:
            result = client.post("/v1/review/responses", headers=_headers("hermes-wa"),
                                 json={"item_refs": ["brn_first", "brn_missing"], "response": "picked_today"})
            assert result.status_code == 404
            assert log.history()[0].response is None
            for refs in ([], "brn_first", [None], [""]):
                result = client.post("/v1/review/responses", headers=_headers("hermes-wa"),
                                     json={"item_refs": refs, "response": "answered"})
                assert result.status_code == 422
    finally:
        runtime.close()


def test_review_plural_marker_validation_keeps_responses_unwritten(tmp_path):
    from gremlin_town.review.why_log import WhyLog
    log = WhyLog(tmp_path / "why.sqlite3")
    now = datetime.now(UTC)
    log.record_surfacings([Surfacing(now, "review", "brn_first", "raw", "question", {},
                                    question="Keep?", offered=("keep",))])
    try:
        result = record_review_response(log, caller="hermes-wa", item_refs=list(log.review_references(now)),
                                        response="answered", response_words="yes", now=now, check_only=True)
        assert result["status"] == "validated" and not result["recorded"]
        assert log.history()[0].response is None
    finally:
        log.close()


@pytest.mark.parametrize("caller", ["hermes-wa", "fleet-boss-grok", "local-codex"])
def test_review_and_day_answers_belong_to_the_door_while_pc_on_stays_open(tmp_path, caller):
    runtime = build_runtime(write_config(tmp_path, review_enabled=True), provider=FakeTransport(), start_worker=False)
    runtime.review_why_log.record_surfacings([Surfacing(datetime.now(UTC), "review", "brn_gate", "a" * 64,
        "question", {}, question="Is that your day?", offered=("accept",))])
    with TestClient(runtime.app) as client:
        for kind in ("day_settled", "day_change"):
            result = client.post("/v1/moments/signals", headers=_headers(caller),
                json={"kind": kind, "detail": '{"deep_focus_minutes":90}' if kind == "day_change" else None})
            assert result.status_code == (200 if caller == "hermes-wa" else 403), result.text
            if caller != "hermes-wa":
                assert result.json()["error"]["fix"] == "Nic settles his day with Diablo"
        result = client.post("/v1/review/responses", headers=_headers(caller),
            json={"item_ref": "brn_gate", "response": "accepted", "response_words": "yes"})
        assert result.status_code == (200 if caller == "hermes-wa" else 403), result.text
        if caller != "hermes-wa":
            assert result.json()["error"]["fix"] == "Nic settles his day with Diablo"
            assert runtime.review_why_log.history()[0].response is None
            assert not runtime.review_why_log.signals_since(0)
        assert client.post("/v1/moments/signals", headers=_headers(caller), json={"kind": "pc_on"}).status_code == 200


def test_work_config_moves_review_gate_without_changing_review_recipient(tmp_path):
    config = write_config(tmp_path, review_enabled=True)
    with config.open("a") as handle:
        handle.write('\n[work]\ndoor_participant = "local-codex"\n')
    runtime = build_runtime(config, provider=FakeTransport(), start_worker=False)
    assert runtime.review.delivery_participant == "hermes-wa"
    with TestClient(runtime.app) as client:
        refused = client.post("/v1/moments/signals", headers=_headers("hermes-wa"), json={"kind": "day_settled"})
        assert refused.status_code == 403 and refused.json()["error"]["fix"]
        assert client.post("/v1/moments/signals", headers=_headers("local-codex"), json={"kind": "day_settled"}).status_code == 200
