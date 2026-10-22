from __future__ import annotations

from datetime import datetime, timezone

from gremlin_town.contracts import Actor, CreateRequest, MessageKind, Receipt, RequestMode
from gremlin_town.review.deliver import deliver


class FakeRequestService:
    def __init__(self) -> None:
        self.calls: list[tuple[Actor, CreateRequest]] = []

    def requests_create(self, actor: Actor, command: CreateRequest) -> Receipt:
        self.calls.append((actor, command))
        return Receipt(
            operation_id="op_1",
            request_id="req_1",
            accepted_at=datetime(2026, 9, 26, 22, 0, tzinfo=timezone.utc),
            operation_state=None,
            work_state=None,
            revision=1,
            actor_id=actor.actor_id,
        )


def test_deliver_creates_action_request_with_rendered_text() -> None:
    service = FakeRequestService()
    receipt = deliver(
        service,
        sender_participant="exosuit-review",
        delivery_participant="hermes-wa",
        project="vault_home",
        client_operation_id="review-2026-09-26",
        objective="Post this review to Nic's Telegram and reply with the message ID",
        text="## Today\n1. Standup - on your calendar",
    )
    assert receipt.request_id == "req_1"
    assert len(service.calls) == 1
    actor, command = service.calls[0]
    assert actor.actor_id == "exosuit-review"
    assert command.recipient == "hermes-wa"
    assert command.project == "vault_home"
    assert command.client_operation_id == "review-2026-09-26"
    assert command.mode == RequestMode.ACTION_REQUESTED
    assert command.packet.kind == MessageKind.REQUEST
    assert command.packet.context == "## Today\n1. Standup - on your calendar"
