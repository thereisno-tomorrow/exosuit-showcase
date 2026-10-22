"""Requested Diablo drafts. GT owns requests; Brain owns only approved work.

Discord identifiers are not authority here. The host binds each interaction or
thread reply to an owner, review day, line and item before calling these methods.
Every operation rechecks that binding and policy. No provider polling occurs.
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import date
import asyncio
import hashlib
import inspect
import json
import re

from starlette.concurrency import run_in_threadpool

from gremlin_town.contracts import Actor, CreateRequest, MessageKind, Packet, RequestMode, WorkState
from gremlin_town.mcp.brain_runtime import WorkRequest
from gremlin_town.service.service import create_digest
from gremlin_town.transport.framing import parse_wire_message


class DiabloRefused(ValueError):
    """Safe words for the card; never include service exceptions or records."""


_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,127}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_BASE = {"day", "line_ref", "item_ref"}
_LABELS = {"research": "Research and break it down", "next": "Draft the next step", "words": "In my own words",
           "nudge": "Draft a nudge"}


def _text(value, limit, message):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or not value.isprintable():
        raise DiabloRefused(message)
    return value


def _key(value):
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise DiabloRefused("Open the card again before trying that tap.")
    return value


def _base(payload):
    if not isinstance(payload, dict):
        raise DiabloRefused("Open the card again before trying that tap.")
    try:
        if date.fromisoformat(payload["day"]).isoformat() != payload["day"]:
            raise ValueError
        _text(payload["line_ref"], 128, "Open the current review item.")
        _text(payload["item_ref"], 128, "Open the current review item.")
    except (KeyError, TypeError, ValueError) as exc:
        raise DiabloRefused("Open the current review item.") from exc


def _expected(value):
    if (not isinstance(value, dict) or set(value) != {"record_id", "sha256"}
            or not isinstance(value["sha256"], str) or not _SHA.fullmatch(value["sha256"])):
        raise DiabloRefused("Refresh this item before asking Diablo.")
    _text(value["record_id"], 128, "Refresh this item before asking Diablo.")
    return value


def _strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def parse_draft(text):
    """A draft can propose only short titles and one detail line, never moves."""
    try:
        if not isinstance(text, str) or len(text.encode()) > 24000:
            raise ValueError
        value = json.loads(text, object_pairs_hook=_strict_object)
        if not isinstance(value, dict) or set(value) != {"steps"}:
            raise ValueError
        steps = value["steps"]
        if not isinstance(steps, list) or not 1 <= len(steps) <= 20:
            raise ValueError
        for step in steps:
            if not isinstance(step, dict) or set(step) != {"title", "detail"}:
                raise ValueError
            _text(step["title"], 120, "Diablo's draft needs a shorter action title.")
            _text(step["detail"], 400, "Diablo's draft needs one short detail line.")
        return steps
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DiabloRefused("Diablo's reply needs short step titles and one detail line each. Reply in the thread to fix it.") from exc


def parse_nudge(text):
    try:
        if not isinstance(text, str) or len(text.encode()) > 12000:
            raise ValueError
        value = json.loads(text, object_pairs_hook=_strict_object)
        if not isinstance(value, dict) or set(value) != {"draft"}:
            raise ValueError
        draft = value["draft"]
        if (not isinstance(draft, str) or not draft.strip() or len(draft) > 2000
                or any(not c.isprintable() and c != "\n" for c in draft)):
            raise ValueError
        return draft
    except (ValueError, TypeError, UnicodeError) as exc:
        raise DiabloRefused("Diablo's reply needs one short follow-up draft. Reply in the thread to fix it.") from exc


class ReviewDiablo:
    def __init__(self, service, *, owner_id, sender_participant, recipient_participant, project,
                 authorize_item, saver, enabled=False):
        self.service, self.owner_id = service, owner_id
        self.actor, self.recipient = Actor(sender_participant), recipient_participant
        self.project, self.authorize_item, self.saver = project, authorize_item, saver
        self.enabled = enabled
        # One gateway owns this adapter. Hold across the entire save so a thread
        # refinement cannot replace the chosen draft between its check and write.
        self._mutations = asyncio.Lock()
        self.prefix = "review-diablo:v1:" + hashlib.sha256(owner_id.encode()).hexdigest() + ":"

    async def _authorize(self, payload, owner_id, *, authorized_item=None):
        if not self.enabled:
            raise DiabloRefused("Ask Diablo is not switched on.")
        _base(payload)
        if owner_id != self.owner_id:
            raise DiabloRefused("This review item is not available.")
        item = authorized_item if authorized_item is not None else self.authorize_item(
            owner_id, payload["day"], payload["line_ref"], payload["item_ref"])
        if inspect.isawaitable(item):
            item = await item
        if not isinstance(item, Mapping) or item.get("ref") != payload["item_ref"]:
            raise DiabloRefused("This review item is no longer available. Open today's review.")
        self.service.policy.require_route(self.project, self.actor.actor_id, self.recipient, "private")
        return item

    def _associated(self, operation, payload):
        if (operation is None or operation.actor_id != self.actor.actor_id or operation.operation_kind != "create"
                or not operation.payload or not operation.client_operation_id.startswith(self.prefix)):
            return False
        wire = parse_wire_message(operation.payload)
        request = self.service.store.request(operation.request_id)
        constraints = wire.packet.constraints
        return bool(request and request.project == self.project and request.requester_id == self.actor.actor_id
                    and request.recipient_id == self.recipient and wire.sender == self.actor.actor_id
                    and wire.recipient == self.recipient and wire.request_id == operation.request_id
                    and wire.operation_id == operation.operation_id and constraints.get("surface") == "review-card"
                    and constraints.get("owner_binding") == self.prefix
                    and all(constraints.get(key) == payload[key] for key in _BASE))

    def _operations(self, payload):
        rows = self.service.store.fetchall(
            """SELECT operation_id FROM operations WHERE actor_id=? AND operation_kind='create'
               AND substr(client_operation_id,1,?)=?
               AND json_extract(CAST(payload AS TEXT),'$.packet.constraints.day')=?
               AND json_extract(CAST(payload AS TEXT),'$.packet.constraints.line_ref')=?
               AND json_extract(CAST(payload AS TEXT),'$.packet.constraints.item_ref')=?
               ORDER BY rowid DESC LIMIT 100""",
            (self.actor.actor_id, len(self.prefix), self.prefix, payload["day"], payload["line_ref"], payload["item_ref"]))
        return [operation for row in rows if self._associated(
            operation := self.service.store.operation(row["operation_id"]), payload)]

    def _result(self, operation):
        return self.service.local_request_result(self.actor, operation.request_id)

    def _summary(self, operation, item):
        wire = parse_wire_message(operation.payload)
        proof = wire.packet.sources[0]
        result = self._result(operation)
        output = dict(state="waiting", operation_id=operation.client_operation_id[len(self.prefix):],
                      request_id=operation.request_id, steps=[], draft_sha256=None,
                      text="Diablo is on it. His draft will show up here.")
        if item.get("record_id") != proof["record_id"] or item.get("record_sha256") != proof["sha256"]:
            output.update(state="stale", text="This item changed. Ask Diablo from its current review item.")
        elif result is not None:
            try:
                if wire.packet.constraints["selected_action"] == "nudge":
                    output.update(state="nudge", text=parse_nudge(result[0]),
                                  draft_sha256=hashlib.sha256(result[0].encode()).hexdigest())
                    return output
                steps = parse_draft(result[0])
                if wire.packet.constraints["selected_action"] == "next" and len(steps) != 1:
                    raise DiabloRefused("Diablo's reply needs one next step. Reply in the thread to fix it.")
                output.update(state="draft", steps=steps, draft_sha256=hashlib.sha256(result[0].encode()).hexdigest(),
                              text="Diablo's suggestion" if len(steps) == 1 else "Diablo's steps")
            except DiabloRefused as exc:
                output.update(state="invalid", text=str(exc))
        elif operation.operation_state.value in {"uncertain", "failed"}:
            output.update(state="delivery_unconfirmed", text="Delivery needs checking. Your request has been kept.")
        else:
            page = self.service.requests_list(self.actor, {"request_id": operation.request_id,
                                                         "include_recovery_held": True, "limit": 1})
            if len(page.items) != 1:
                raise DiabloRefused("This request is no longer available.")
            snapshot = page.items[0]
            if snapshot["recovery_hold"] or snapshot["work_state"] in {WorkState.DECLINED, WorkState.CANCELLED}:
                output.update(state="unavailable", text="Diablo could not complete this request. Your item is unchanged.")
        return output

    def _command(self, payload, previous=None):
        action = payload.get("action") or parse_wire_message(previous.payload).packet.constraints["selected_action"]
        constraints = {"surface": "review-card", "owner_binding": self.prefix,
                       **{key: payload[key] for key in _BASE}, "selected_action": action}
        if previous is not None:
            reply = self._result(previous)
            if reply is None:
                raise DiabloRefused("Wait for Diablo's draft before refining it.")
            constraints.update(previous_request_id=previous.request_id, previous_reply=reply[0])
        packet = Packet(1, MessageKind.REQUEST, None, _LABELS[action], payload["text"], constraints,
                        ({"type": "brain_item", "ref": payload["item_ref"], **payload["expected"]},),
                        ("Draft one short follow-up for the owner to use. " if action == "nudge" else
                         "Research and draft only. Return short action titles and one detail line per step. ") +
                        "Save nothing to Brain Home, make no calendar changes and send no external messages. "
                        + ("Return only the follow-up draft; this action does not save or send it." if action == "nudge" else
                           "The owner approves the draft with a separate Save tap. "
                           + ("Return exactly one next step." if action == "next" else "Return one to twenty steps.")),
                        'JSON only: {"draft":"One short follow-up text"}' if action == "nudge" else
                        'JSON only: {"steps":[{"title":"Short action title","detail":"One detail line"}]}', "private")
        return CreateRequest(self.prefix + payload["operation_id"], RequestMode.ACTION_REQUESTED,
                             self.project, self.recipient, packet)

    def _send(self, payload, item, *, refining=False):
        previous = None
        if refining:
            previous = self.service.store.operation_by_client(self.actor.actor_id, self.prefix + payload["previous_operation_id"])
            if not self._associated(previous, payload):
                raise DiabloRefused("Reply to the thread for this review item.")
        command = self._command(payload, previous)
        existing = self.service.store.operation_by_client(self.actor.actor_id, command.client_operation_id)
        if existing is not None:
            if not self._associated(existing, payload) or existing.input_digest != create_digest(command):
                raise DiabloRefused("This retry has different words. Open the earlier request.")
        else:
            if item.get("record_id") != payload["expected"]["record_id"] or item.get("record_sha256") != payload["expected"]["sha256"]:
                raise DiabloRefused("This item changed. Open it again before asking Diablo.")
            offered = set(item.get("offered", ()))
            action = command.packet.constraints["selected_action"]
            if (action == "nudge" and "draft a nudge" not in offered
                    or action != "nudge" and (not offered.intersection({"parts", "add_parts", "next_action"})
                        or action == "research" and not offered.intersection({"parts", "add_parts"}))):
                raise DiabloRefused("This item does not offer that draft. Choose its current answer.")
            latest = self._operations(payload)
            if refining and (not latest or latest[0].operation_id != previous.operation_id):
                raise DiabloRefused("A newer request exists. Reply to its latest draft.")
            if not refining and latest:
                raise DiabloRefused("Diablo already has this item. Continue in its thread.")
        receipt = self.service.requests_create(self.actor, command)
        operation = self.service.store.operation_by_client(self.actor.actor_id, command.client_operation_id)
        if receipt.request_id is None or not self._associated(operation, payload):
            raise RuntimeError("Review Diablo request persistence was not confirmed")
        return self._summary(operation, item)

    async def send(self, payload, *, owner_id):
        self._validate_send(payload)
        async with self._mutations:
            item = await self._authorize(payload, owner_id)
            return await run_in_threadpool(self._send, payload, item)

    @staticmethod
    def _validate_send(payload, refining=False):
        _base(payload)
        extra = {"previous_operation_id"} if refining else {"action"}
        if set(payload) != _BASE | {"operation_id", "expected", "text"} | extra:
            raise DiabloRefused("The request has missing or unexpected fields. Open the item again.")
        _key(payload["operation_id"])
        _expected(payload["expected"])
        if refining:
            _key(payload["previous_operation_id"])
        elif not isinstance(payload["action"], str) or payload["action"] not in _LABELS:
            raise DiabloRefused("Choose what Diablo should do.")
        text = payload["text"]
        if not isinstance(text, str) or len(text) > 4000 or "\x00" in text:
            raise DiabloRefused("Use at most 4,000 characters for your words.")
        if (refining or payload.get("action") == "words") and not text.strip():
            raise DiabloRefused("Write what you want Diablo to change.")
        if not refining and payload["action"] != "words" and text:
            raise DiabloRefused("Use In my own words to send your own instruction.")

    async def refine(self, payload, *, owner_id):
        self._validate_send(payload, refining=True)
        async with self._mutations:
            item = await self._authorize(payload, owner_id)
            return await run_in_threadpool(self._send, payload, item, refining=True)

    async def read(self, payload, *, owner_id, authorized_item=None):
        """The card may supply its just-authorized render item; standalone reads check anew.

        This internal argument is never accepted by the Discord or GT transport.
        Sends, refinements and saves always run their independent authorization.
        """
        _base(payload)
        if set(payload) != _BASE:
            raise DiabloRefused("Open this review item again.")
        item = await self._authorize(payload, owner_id, authorized_item=authorized_item)
        def read():
            operations = self._operations(payload)
            return self._summary(operations[0], item) if operations else {"state": "none", "steps": []}
        return await run_in_threadpool(read)

    async def revision(self, day):
        """Internal poll fingerprint: local GT only, no Brain/calendar reads.

        A changed fingerprint means the host should perform its normal freshly
        authorized redraw before exposing anything. This never returns content.
        """
        if not self.enabled:
            return "disabled"
        _base({"day": day, "line_ref": "poll", "item_ref": "poll"})
        def revision():
            self.service.policy.require_route(self.project, self.actor.actor_id, self.recipient, "private")
            rows = self.service.store.fetchall(
                """SELECT operation_id FROM operations WHERE actor_id=? AND operation_kind='create'
                   AND substr(client_operation_id,1,?)=?
                   AND json_extract(CAST(payload AS TEXT),'$.packet.constraints.day')=? ORDER BY rowid""",
                (self.actor.actor_id, len(self.prefix), self.prefix, day))
            values = []
            for row in rows:
                operation = self.service.store.operation(row["operation_id"])
                wire = parse_wire_message(operation.payload)
                binding = {key: wire.packet.constraints.get(key) for key in _BASE}
                if not self._associated(operation, binding):
                    continue
                page = self.service.requests_list(self.actor, {"request_id": operation.request_id,
                                                             "include_recovery_held": True, "limit": 1})
                if len(page.items) != 1:
                    raise DiabloRefused("This request is no longer available.")
                snapshot = page.items[0]
                values.append([operation.operation_id, operation.operation_state.value,
                               snapshot["revision"], snapshot["work_state"], snapshot["recovery_hold"]])
            return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        return await run_in_threadpool(revision)

    async def savedraft(self, payload, *, owner_id):
        async with self._mutations:
            change = await self._prepare_save(payload, owner_id=owner_id)
            result = self.saver(change, owner_id=owner_id)
            return await result if inspect.isawaitable(result) else result

    async def prepare_save(self, payload, *, owner_id):
        """Freeze a checked draft before the card journals its save operation.

        The host persists this exact WorkRequest before publication, then retries
        the ordinary checked door directly after an uncertain save reply. That
        door still refreshes owner policy and validates the original proof.
        """
        async with self._mutations:
            return await self._prepare_save(payload, owner_id=owner_id)

    async def _prepare_save(self, payload, *, owner_id):
        _base(payload)
        if set(payload) != _BASE | {"operation_id", "draft_operation_id", "draft_sha256"}:
            raise DiabloRefused("Open the latest draft before saving it.")
        _key(payload["operation_id"])
        _key(payload["draft_operation_id"])
        if not isinstance(payload["draft_sha256"], str) or not _SHA.fullmatch(payload["draft_sha256"]):
            raise DiabloRefused("Open the latest draft before saving it.")
        item = await self._authorize(payload, owner_id)
        def request():
            operations = self._operations(payload)
            if not operations or operations[0].client_operation_id != self.prefix + payload["draft_operation_id"]:
                raise DiabloRefused("A newer draft is being prepared. Save its latest version.")
            operation = operations[0]
            if parse_wire_message(operation.payload).packet.constraints["selected_action"] == "nudge":
                raise DiabloRefused("A follow-up draft is for you to use. It does not save or send anything.")
            result = self._result(operation)
            if result is None or hashlib.sha256(result[0].encode()).hexdigest() != payload["draft_sha256"]:
                raise DiabloRefused("The draft changed. Open its latest version before saving.")
            steps = parse_draft(result[0])
            wire = parse_wire_message(operation.payload)
            if wire.packet.constraints["selected_action"] == "next" and len(steps) != 1:
                raise DiabloRefused("Diablo's reply needs one next step.")
            proof = {key: wire.packet.sources[0][key] for key in ("record_id", "sha256")}
            approval = {"request_id": operation.request_id, "sha256": payload["draft_sha256"], "author": "Diablo"}
            offered = item.get("offered", ())
            if "parts" in offered or "add_parts" in offered:
                move = {"move": "parts", "thing": payload["item_ref"],
                        "parts": [{"title": step["title"], "details": step["detail"]} for step in steps],
                        "approved_draft": approval}
            elif "next_action" in offered and len(steps) == 1:
                move = {"move": "next_action", "thing": payload["item_ref"], "text": steps[0]["title"],
                        "details": steps[0]["detail"], "approved_draft": approval}
            else:
                raise DiabloRefused("This review item does not offer that draft save. Open its current question.")
            return WorkRequest(payload["operation_id"], "Save this step" if len(steps) == 1 else "Save these steps",
                               "owner", "direct", (move,), answers=(payload["line_ref"],),
                               expected_records={payload["item_ref"]: proof})
        return await run_in_threadpool(request)
