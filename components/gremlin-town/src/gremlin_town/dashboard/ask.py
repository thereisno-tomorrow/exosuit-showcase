"""Owner-selected Diablo requests, persisted by GT's existing request writer.

The dashboard stores no duplicate request or record. GT's frozen create payload
holds the item/owner association and exact words; its operation key owns retries.
"""
from __future__ import annotations

import hashlib
import inspect
import re
from collections.abc import Callable, Mapping

from starlette.concurrency import run_in_threadpool

from gremlin_town.contracts import Actor, CreateRequest, MessageKind, Packet, RequestMode, WorkState
from gremlin_town.service.service import create_digest
from gremlin_town.transport.framing import parse_wire_message


class AskRefused(ValueError):
    def __init__(self, text: str, status: int = 422):
        super().__init__(text)
        self.status = status


_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,127}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_LABELS = {"get_ready": "Get it ready", "do_it": "Do it"}


def _ref(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ord(c) < 32 for c in value):
        raise AskRefused("Open this thing again before asking Diablo.")
    return value


def validate(payload):
    if not isinstance(payload, dict) or set(payload) != {"operation_id", "ref", "expected", "action", "text"}:
        raise AskRefused("The request has missing or unexpected fields. Reopen this thing.")
    if not isinstance(payload["operation_id"], str) or not _KEY.fullmatch(payload["operation_id"]):
        raise AskRefused("The retry key is not valid. Reopen this thing.")
    _ref(payload["ref"])
    if not isinstance(payload["action"], str) or payload["action"] not in _LABELS:
        raise AskRefused("Choose Get it ready or Do it.")
    text = payload["text"]
    if not isinstance(text, str) or len(text) > 4000 or "\x00" in text:
        raise AskRefused("Use at most 4,000 characters for your words.")
    expected = payload["expected"]
    if (not isinstance(expected, dict) or set(expected) != {"record_id", "sha256"}
            or not isinstance(expected["record_id"], str) or not 1 <= len(expected["record_id"]) <= 128
            or not isinstance(expected["sha256"], str) or not _HASH.fullmatch(expected["sha256"])):
        raise AskRefused("Refresh this thing before asking Diablo.")
    return payload


class DashboardAsk:
    def __init__(self, service, *, owner_id: str, sender_participant: str,
                 recipient_participant: str, project: str, authorize_item: Callable):
        self.service = service
        self.owner_id = owner_id
        self.actor = Actor(sender_participant)
        self.recipient = recipient_participant
        self.project = project
        self.authorize_item = authorize_item
        # The browser cannot select an actor, route, scope, sensitivity or owner.
        self.prefix = "dashboard-ask:v1:" + hashlib.sha256(owner_id.encode()).hexdigest() + ":"

    async def _authorize(self, owner_id, ref):
        if owner_id != self.owner_id:
            raise AskRefused("This thing is not available.", 403)
        _ref(ref)
        item = self.authorize_item(owner_id, ref)
        if inspect.isawaitable(item):
            item = await item
        if not isinstance(item, Mapping) or item.get("ref") != ref:
            raise AskRefused("This thing is not available. Refresh your work.", 404)
        # The callback must recheck the Brain owner policy on every call.
        self.service.policy.require_route(self.project, self.actor.actor_id, self.recipient, "private")
        return item

    def _command(self, payload):
        label = _LABELS[payload["action"]]
        packet = Packet(
            schema_version=1, kind=MessageKind.REQUEST, session_reference=None,
            objective=label, context=payload["text"],
            constraints={"surface": "dashboard", "owner_binding": self.prefix,
                         "item_ref": payload["ref"], "selected_action": payload["action"]},
            sources=({"type": "brain_item", "ref": payload["ref"], **payload["expected"]},),
            next_action=label, expected_reply="Return the result to this Gremlin Town request.",
            sensitivity="private",
        )
        return CreateRequest(client_operation_id=self.prefix + payload["operation_id"],
                             mode=RequestMode.ACTION_REQUESTED, project=self.project,
                             recipient=self.recipient, packet=packet)

    def _associated(self, operation, ref):
        if (operation is None or operation.actor_id != self.actor.actor_id
                or operation.operation_kind != "create" or not operation.payload
                or not operation.client_operation_id.startswith(self.prefix)):
            return False
        wire = parse_wire_message(operation.payload)
        request = self.service.store.request(operation.request_id)
        return bool(request and request.project == self.project
                    and request.requester_id == self.actor.actor_id and request.recipient_id == self.recipient
                    and wire.sender == self.actor.actor_id and wire.recipient == self.recipient
                    and wire.request_id == operation.request_id and wire.operation_id == operation.operation_id
                    and wire.packet.constraints.get("surface") == "dashboard"
                    and wire.packet.constraints.get("owner_binding") == self.prefix
                    and wire.packet.constraints.get("item_ref") == ref)

    async def send(self, payload, *, owner_id):
        payload = validate(payload)
        item = await self._authorize(owner_id, payload["ref"])
        return await run_in_threadpool(self._send, payload, item)

    def _send(self, payload, item):
        command = self._command(payload)
        existing = self.service.store.operation_by_client(self.actor.actor_id, command.client_operation_id)
        if existing:
            if not self._associated(existing, payload["ref"]) or existing.input_digest != create_digest(command):
                raise AskRefused("This retry has different words. Check the earlier request first.", 409)
        elif (item.get("record_id") != payload["expected"]["record_id"]
              or item.get("record_sha256") != payload["expected"]["sha256"]):
            raise AskRefused("This thing changed. Refresh it before asking Diablo.", 409)
        # GT atomically reserves the exact payload and key before its outbox sends.
        # An exception after commit is safely retried with this same command.
        receipt = self.service.requests_create(self.actor, command)
        operation = self.service.store.operation_by_client(self.actor.actor_id, command.client_operation_id)
        if receipt.request_id is None or not self._associated(operation, payload["ref"]):
            raise RuntimeError("Dashboard request persistence was not confirmed")
        return self._summary(operation)

    async def read(self, ref, *, owner_id):
        await self._authorize(owner_id, ref)
        return await run_in_threadpool(self._read, ref)

    def _read(self, ref):
        # Exact namespace/item matching prevents guessed GT IDs from becoming a
        # read API. Local associations are metadata inside the canonical outbox.
        rows = self.service.store.fetchall(
            """SELECT operation_id FROM operations WHERE actor_id=? AND operation_kind='create'
               AND substr(client_operation_id,1,?)=?
               AND json_extract(CAST(payload AS TEXT),'$.packet.constraints.item_ref')=?
               ORDER BY accepted_at DESC, operation_id DESC LIMIT 20""",
            (self.actor.actor_id, len(self.prefix), self.prefix, ref),
        )
        output = []
        for row in rows:
            operation = self.service.store.operation(row["operation_id"])
            if self._associated(operation, ref):
                output.append(self._summary(operation, include_result=True))
        return {"requests": output}

    def _summary(self, operation, *, include_result=False):
        # This policy-checked service read is local metadata only. The accepted
        # result below already lives in GT's canonical outbox; fetching provider
        # threads here would turn every browser poll into network traffic/events.
        page = self.service.requests_list(self.actor, {"request_id": operation.request_id,
                                                      "include_recovery_held": True, "limit": 1})
        if len(page.items) != 1:
            raise AskRefused("This request is not available.", 403)
        snapshot = page.items[0]
        wire = parse_wire_message(operation.payload)
        state = operation.operation_state.value
        result = None
        if include_result and snapshot["work_state"] == WorkState.RESULT_RETURNED:
            # Only the recipient's accepted, hub-attributed result may be shown.
            # Untrusted comments and pending/uncertain reply payloads are excluded.
            row = self.service.store.fetchone(
                """SELECT op.operation_id FROM events ev JOIN operations op ON ev.operation_id=op.operation_id
                   WHERE ev.request_id=? AND ev.event_type='result_returned'
                   AND op.actor_id=? AND op.operation_state='accepted' AND op.message_kind='result'
                   ORDER BY ev.sequence DESC LIMIT 1""", (operation.request_id, self.recipient))
            if row:
                reply = self.service.store.operation(row["operation_id"])
                returned = parse_wire_message(reply.payload)
                if (returned.request_id == operation.request_id and returned.sender == self.recipient
                        and returned.recipient == self.actor.actor_id and returned.packet.kind == MessageKind.RESULT):
                    result = {"text": returned.packet.context, "objective": returned.packet.objective}
        text = "Sent to Diablo"
        if snapshot["recovery_hold"]:
            text = "Delivery is on hold. It needs checking."
        elif state in {"uncertain", "failed"}:
            text = "Delivery needs checking. The request has been kept."
        elif snapshot["work_state"] == WorkState.CANCELLED:
            text = "Cancelled"
        elif snapshot["work_state"] == WorkState.DECLINED:
            text = "Diablo declined this request"
        elif result is not None:
            text = "Diablo's reply"
        return {"operation_id": operation.client_operation_id[len(self.prefix):],
                "action": wire.packet.constraints["selected_action"], "words": wire.packet.context,
                "expected": {key: wire.packet.sources[0][key] for key in ("record_id", "sha256")},
                "text": text, "delivery": state, "state": snapshot["work_state"],
                "result": result, "created_at": operation.accepted_at.isoformat()}
