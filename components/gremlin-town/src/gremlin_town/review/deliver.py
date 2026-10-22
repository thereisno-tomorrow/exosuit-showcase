"""Hands the rendered review or moment text to Diablo as one GT request (§3, §4).

Exosuit code never talks to Telegram (D7): this creates one action request
from the review's sender participant to the delivery participant (hermes-wa),
carrying the rendered text and a replay-safe `client_operation_id` so a rerun
with the same id sends nothing new (`requests_create`'s own dedup).
"""

from __future__ import annotations

from typing import Any, Mapping, Protocol

from gremlin_town.contracts import Actor, CreateRequest, MessageKind, Packet, Receipt, RequestMode

PACKET_SCHEMA_VERSION = 1


class RequestCreator(Protocol):
    def requests_create(self, actor: Actor, command: CreateRequest) -> Receipt: ...


def deliver(
    service: RequestCreator,
    *,
    sender_participant: str,
    delivery_participant: str,
    project: str,
    client_operation_id: str,
    objective: str,
    text: str,
    next_action: str = "Post the text to Nic's Telegram, verbatim",
    expected_reply: str = "The Telegram message ID once posted",
    sensitivity: str = "private",
    constraints: Mapping[str, Any] | None = None,
) -> Receipt:
    """`constraints` carries structured data for the recipient, never text to post: today
    only the review's clarifying-question picks (step E spec §3.1). Omitted, it is `{}`."""

    actor = Actor(actor_id=sender_participant)
    packet = Packet(
        schema_version=PACKET_SCHEMA_VERSION,
        kind=MessageKind.REQUEST,
        session_reference=None,
        objective=objective,
        context=text,
        constraints=dict(constraints or {}),
        sources=(),
        next_action=next_action,
        expected_reply=expected_reply,
        sensitivity=sensitivity,
    )
    command = CreateRequest(
        client_operation_id=client_operation_id,
        mode=RequestMode.ACTION_REQUESTED,
        project=project,
        recipient=delivery_participant,
        packet=packet,
    )
    return service.requests_create(actor, command)
