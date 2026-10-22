"""Separate pre-review job; no Brain Home writes, no model calls in core."""
import json
import logging
import sqlite3
from dataclasses import asdict
from datetime import datetime, time

from brain_core.review import list_name_candidates
from brain_core.timewords import SINGAPORE
from brain_core import work
from gremlin_town.contracts import Actor, ErrorCode, MAX_PACKET_BYTES, MessageKind, Packet
from gremlin_town.domain.errors import GremlinTownError
from gremlin_town.domain.serialization import canonical_bytes, packet_bytes
from gremlin_town.review.collect import SourceUnavailable
from gremlin_town.review.deliver import PACKET_SCHEMA_VERSION, deliver
from gremlin_town.review.why_log import WhyLogError

logger = logging.getLogger(__name__)


def _bound_request(request):
    """Trim the largest context fields before freezing, preserving the candidate title."""
    constraints = request["constraints"]
    candidate = constraints["name_candidate"]
    candidate["tasks"] = list(candidate["tasks"])
    candidate["labels"] = list(candidate["labels"])
    fields = ((candidate, "first_words"), (candidate, "tasks"), (candidate, "labels"),
              (constraints, "project_name_examples"))
    while True:
        packet = Packet(PACKET_SCHEMA_VERSION, MessageKind.REQUEST, None, request["objective"],
                        request["text"], constraints, (), request["next_action"],
                        request["expected_reply"], "private")
        frozen_size = len(json.dumps(request, sort_keys=True).encode("utf-8"))
        if max(len(packet_bytes(packet)), frozen_size) < MAX_PACKET_BYTES:
            return
        remaining = [(parent, key) for parent, key in fields if parent[key]]
        if not remaining:
            raise GremlinTownError(ErrorCode.PACKET_TOO_LARGE, "name request cannot fit with its title")
        parent, key = max(remaining, key=lambda field: len(canonical_bytes(field[0][field[1]])))
        value = parent[key]
        if isinstance(value, str):
            parent[key] = value[:len(value) // 2]
        else:
            longest = max(range(len(value)), key=lambda index: len(canonical_bytes(value[index])))
            if value[longest]:
                value[longest] = value[longest][:len(value[longest]) // 2]
            else:
                value.pop(longest)


def run_names(context, now):
    if context.settings.tidy:
        from gremlin_town.review.tidy import run_tidy
        return run_tidy(context, now)
    if not context.settings.enabled:
        return {"outcome": "disabled"}
    if context.why_log is None:
        raise RuntimeError("review names requires the review why-log")
    local = now.astimezone(SINGAPORE)
    if not time.fromisoformat(context.settings.names_at) <= local.time().replace(tzinfo=None) < time(6):
        return {"outcome": "outside_window"}
    reader = context.sources.brain_items
    if reader is None:
        raise SourceUnavailable("brain items not configured")
    items = tuple(reader())
    candidates = list_name_candidates(items, now)
    if not candidates:
        return {"outcome": "empty", "count": 0}
    settings = context.settings
    examples = [record.title for record in work.index_things(entry.record for entry in items).values()
                if record.data.get("called") == "project" and not record.data.get("level") == "suggested"
                and not any(m.get("mark") == "agents_wording" and m.get("part") == "title"
                            for m in record.data.get("review_marks", ()))]
    day = local.date()
    sent = 0
    for candidate in candidates:
        request = dict(sender_participant=settings.sender_participant,
            delivery_participant=settings.delivery_participant, project=settings.project or "",
            client_operation_id=f"name-{day.isoformat()}-{candidate.item_ref}",
            objective="Suggest one name for the review's name question",
            text="Use the thing's context in constraints.name_candidate and the project-name examples.",
            constraints={"name_candidate": asdict(candidate), "project_name_examples": examples[:10],
                         "deadline": datetime.combine(day, time(6), SINGAPORE).isoformat()},
            next_action="Outside Nic's chat, suggest ONE short, plain name in his style, using the context and examples. "
                        "Return only the requested JSON. Post nothing to Nic; save nothing to Brain Home. "
                        "Do not suggest parts, next actions or dates.",
            expected_reply='Exactly JSON {"suggested_name":"one name"}; no markdown or explanation')
        try:
            _bound_request(request)
            prepared = context.why_log.prepare_name_request(day, candidate.item_ref, candidate.record_sha256, now, request)
            if prepared["request_id"] is not None:
                continue
            receipt = deliver(context.service, **prepared["request"])
            if receipt.error is not None:
                logger.warning("%s %s", request["client_operation_id"], receipt.error.code.value)
                continue
            context.why_log.record_name_request(day, candidate.item_ref, receipt.request_id)
        except (GremlinTownError, WhyLogError, OSError, sqlite3.Error) as exc:
            code = exc.code if isinstance(exc, GremlinTownError) else ErrorCode.SOURCE_UNAVAILABLE
            logger.warning("%s %s", request["client_operation_id"], code.value)
            continue
        sent += 1
    return {"outcome": "requested", "count": len(candidates), "sent": sent}


def read_names(context, now):
    """Only on-time, attributed local results; missing, invalid and late answers add nothing."""
    if context.settings.tidy:
        return {}
    reader = getattr(context.service, "local_request_result", None)
    if reader is None or context.why_log is None:
        return {}
    day = now.astimezone(SINGAPORE).date()
    cutoff = datetime.combine(day, time(6), SINGAPORE)
    names = {}
    for row in context.why_log.name_requests(day):
        if not row["request_id"]:
            continue
        try:
            result = reader(Actor(actor_id=row["request"]["sender_participant"]), row["request_id"])
        except (GremlinTownError, OSError, sqlite3.Error) as exc:
            code = exc.code if isinstance(exc, GremlinTownError) else ErrorCode.SOURCE_UNAVAILABLE
            logger.warning("%s %s", row["request_id"], code.value)
            continue
        if result is None:
            continue
        text, returned_at = result
        if not datetime.fromisoformat(row["prepared_at"]) <= returned_at < cutoff or returned_at > now:
            continue
        try:
            value = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            logger.warning("Invalid name result for %s: expected JSON", row["request_id"])
            continue
        name = value.get("suggested_name") if isinstance(value, dict) and set(value) == {"suggested_name"} else None
        if not isinstance(name, str) or not name.strip() or len(name) > 256 or not name.isprintable():
            logger.warning("Invalid name result for %s: expected one plain name of 1-256 characters", row["request_id"])
            continue
        names[row["item_ref"]] = (row["record_sha256"], name.strip())
    return names
