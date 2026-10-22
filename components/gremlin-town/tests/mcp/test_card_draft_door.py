"""The accepted GT draft crosses the real checked Brain door only on the tap."""
import asyncio
import hashlib
import json

import pytest

from brain_core import work
from gremlin_town.contracts import Actor, MessageKind, Packet, ReplyRequest
from gremlin_town.mcp.brain_runtime import BrainRuntimeError, WorkRequest
from gremlin_town.mcp.host_identity import bind_write_attribution
from gremlin_town.mcp.surface_door import bind_owner_tap
from gremlin_town.review.why_log import Surfacing
from gremlin_town.review_card.diablo import ReviewDiablo
from tests.dashboard.test_ask import hub as gt_hub
from test_work_door import NOW, WORK, setup_door, call, records, request


@pytest.mark.parametrize("kind", ["parts", "next_action"])
@pytest.mark.parametrize("stale", [False, True])
def test_accepted_draft_retains_attribution_and_replays_through_real_door(tmp_path, gt_hub, kind, stale):
    provider, fake, _, log = setup_door(tmp_path)
    provider.code_callers = frozenset({"system:review-card"})
    try:
        call(provider, request())
        original = records(fake)[0]
        log.record_surfacings([Surfacing(NOW, "review", original.id, original.sha256, "needs_you", {},
            question="What is the next fixture step?", offered=(kind,))])
        reference = log.review_references(NOW)[0]
        async def authorize(owner, day, line, ref):
            assert owner == WORK.owner_id and line == reference and ref == original.id
            item = work.index_things(records(fake))[ref]
            raw = next(record for record in records(fake) if record.id == item.id)
            return {"ref": ref, "record_id": raw.id, "record_sha256": raw.sha256, "offered": (kind,)}
        async def saver(change, *, owner_id):
            with bind_owner_tap("system:review-card", owner_id, change), bind_write_attribution("system:review-card"):
                return await provider.work(WORK, change)
        api = ReviewDiablo(gt_hub.service, owner_id=WORK.owner_id, sender_participant="fixture-sender",
            recipient_participant="fixture-recipient", project="fixture-project", authorize_item=authorize,
            saver=saver, enabled=True)
        base = {"day": NOW.date().isoformat(), "line_ref": reference, "item_ref": original.id}
        sent = asyncio.run(api.send({**base, "operation_id": "fixture-draft", "action": "next", "text": "",
            "expected": {"record_id": original.id, "sha256": original.sha256}}, owner_id=WORK.owner_id))
        gt_hub.worker.dispatch_once()
        gt_request = gt_hub.store.request(sent["request_id"])
        draft = json.dumps({"steps": [{"title": "Inspect the fixture receipt", "detail": "Use the latest fixture statement"}]})
        gt_hub.service.requests_reply(Actor("fixture-recipient"), ReplyRequest("fixture-result", gt_request.request_id,
            gt_request.revision, MessageKind.RESULT, Packet(1, MessageKind.RESULT, None, "Fixture", draft, {}, (), "", "", "private")))
        gt_hub.worker.dispatch_once()
        assert len(records(fake)) == 1  # the model did not write anything
        answer = {**base, "operation_id": "fixture-save-draft", "draft_operation_id": "fixture-draft",
                  "draft_sha256": hashlib.sha256(draft.encode()).hexdigest()}
        if stale:
            call(provider, WorkRequest("fixture-other-change", "It takes 30 minutes", "owner", "direct",
                ({"move": "organise", "thing": original.id, "rough_minutes": 30},)))
            head = fake.branch
            before_answer = log.answer_context(reference)
            with pytest.raises(BrainRuntimeError, match="changed"):
                asyncio.run(api.savedraft(answer, owner_id=WORK.owner_id))
            assert fake.branch == head and log.answer_context(reference) == before_answer
            return
        saved = asyncio.run(api.savedraft(answer, owner_id=WORK.owner_id))
        assert saved["status"] == "accepted"
        head = fake.branch
        assert asyncio.run(api.savedraft(answer, owner_id=WORK.owner_id))["status"] == "duplicate"
        assert fake.branch == head
        versions = list(work.index_things(records(fake)).values())
        changed = next(record for record in versions if record.data.get("parent")) if kind == "parts" else versions[0]
        assert changed.source["words"] == "Save this step"
        assert changed.evidence[-2].actor == "Diablo" and changed.evidence[-2].content_sha256 == answer["draft_sha256"]
        assert changed.evidence[-1].actor == "system:review-card"
        assert not changed.data.get("review_marks")
        assert not log.answer_context(reference).open
    finally:
        log.close()


def test_approved_metadata_never_authorizes_generic_agent_call(tmp_path):
    provider, fake, _, log = setup_door(tmp_path)
    try:
        call(provider, request())
        original = records(fake)[0]
        head = fake.branch
        with pytest.raises(BrainRuntimeError):
            call(provider, WorkRequest("forged-approval", "Save this step", "owner", "direct", ({"move": "parts",
                "thing": original.id, "parts": [{"title": "Invented", "details": "Invented"}],
                "approved_draft": {"request_id": "fictional", "sha256": "a" * 64, "author": "Diablo"}},)))
        assert fake.branch == head
    finally:
        log.close()
