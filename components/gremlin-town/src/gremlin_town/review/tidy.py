"""One pre-review tidy request. Proposals stay in the hub log, never Brain Home."""
from dataclasses import asdict
from datetime import datetime, time, timedelta
import hashlib
import json
import logging
import re
import sqlite3

from brain_core.review import list_tidy_candidates
from brain_core.timewords import SINGAPORE
from gremlin_town.contracts import Actor, ErrorCode, MAX_PACKET_BYTES, MessageKind, Packet
from gremlin_town.domain.errors import GremlinTownError
from gremlin_town.domain.serialization import packet_bytes
from gremlin_town.review.collect import SourceUnavailable, collect_morning_inputs
from gremlin_town.review.deliver import PACKET_SCHEMA_VERSION, deliver

logger = logging.getLogger(__name__)
_BATCH = "__tidy_batch__"
_FIELDS = frozenset({"suggested_title", "suggested_label", "suggested_name"})
_ADDED_BATCH = "__added_tidy_batch__:"


def _bound(request):
    candidates = request["constraints"]["tidy_candidates"]
    while True:
        packet = Packet(PACKET_SCHEMA_VERSION, MessageKind.REQUEST, None, request["objective"], request["text"],
                        request["constraints"], (), request["next_action"], request["expected_reply"], "private")
        if max(len(packet_bytes(packet)), len(json.dumps(request).encode())) < MAX_PACKET_BYTES:
            return
        fields = [(candidate, key) for candidate in candidates for key in ("first_words", "tasks", "labels")
                  if candidate[key]]
        if not fields:
            raise GremlinTownError(ErrorCode.PACKET_TOO_LARGE, "Tidy request cannot fit its candidate titles")
        candidate, key = max(fields, key=lambda pair: len(json.dumps(pair[0][pair[1]]).encode()))
        value = candidate[key]
        candidate[key] = value[:len(value) // 2]


def run_tidy(context, now):
    if not context.settings.enabled or not context.settings.tidy:
        return {"outcome": "disabled"}
    if context.why_log is None:
        raise RuntimeError("review tidy requires the review why-log")
    local = now.astimezone(SINGAPORE)
    if not time.fromisoformat(context.settings.names_at) <= local.time().replace(tzinfo=None) < time(6):
        return {"outcome": "outside_window"}
    log, day = context.why_log, local.date()
    frozen = next((row for row in log.name_requests(day) if row["item_ref"] == _BATCH), None)
    if frozen is None:
        inputs = collect_morning_inputs(context.sources, log.morning_history(), now, ranking=context.ranking,
            previous_review_at=log.last_sent_review_at(), bootstrap=log.morning_bootstrap(),
            shown_item_refs=log.shown_item_refs())
        if inputs.source_status.get("brain") is not None:
            raise SourceUnavailable("brain items unavailable for tidy")
        candidates = [asdict(candidate) for candidate in list_tidy_candidates(inputs)]
        if not candidates:
            return {"outcome": "empty", "count": 0}
        request = dict(sender_participant=context.settings.sender_participant,
            delivery_participant=context.settings.delivery_participant, project=context.settings.project or "",
            client_operation_id=f"tidy-{day.isoformat()}", objective="Tidy the morning review's new things",
            text="Use only the original words and context in constraints.tidy_candidates.",
            constraints={"tidy_candidates": candidates, "deadline": datetime.combine(day, time(6), SINGAPORE).isoformat()},
            next_action="Suggest a short action title and one label for each thing. For a project-like thing, also suggest "
                "one short name. Use null when its existing words are already clear. Do not invent facts, dates or steps. "
                "These are proposals: save nothing to Brain Home and post nothing to Nic. Return only the requested JSON.",
            expected_reply='JSON {"suggestions":[{"item_ref":"the supplied reference","suggested_title":null,'
                '"suggested_label":null,"suggested_name":null}]} with every supplied reference once.')
        _bound(request)
        digest = hashlib.sha256(json.dumps(candidates, sort_keys=True).encode()).hexdigest()
        frozen = log.prepare_name_request(day, _BATCH, digest, now, request)
    if frozen["request_id"] is not None:
        return {"outcome": "already_requested"}
    receipt = deliver(context.service, **frozen["request"])
    if receipt.error is not None:
        raise GremlinTownError(receipt.error.code, "The pre-review tidy request was not delivered")
    log.record_name_request(day, _BATCH, receipt.request_id)
    return {"outcome": "requested", "count": len(frozen["request"]["constraints"]["tidy_candidates"]), "sent": 1}


def read_tidy(context, now):
    if not context.settings.tidy or context.why_log is None:
        return {}

    reader = getattr(context.service, "local_request_result", None)
    if reader is None:
        return {}
    day = now.astimezone(SINGAPORE).date()
    row = next((row for row in context.why_log.name_requests(day) if row["item_ref"] == _BATCH), None)
    if row is None or not row["request_id"]:
        return {}
    try:
        result = reader(Actor(actor_id=row["request"]["sender_participant"]), row["request_id"])
    except (GremlinTownError, OSError, sqlite3.Error):
        logger.warning("Pre-review tidy result unavailable; review continues without suggestions")
        return {}
    if result is None:
        return {}
    text, returned_at = result
    if not datetime.fromisoformat(row["prepared_at"]) <= returned_at < datetime.combine(day, time(6), SINGAPORE) or returned_at > now:
        return {}
    candidates = {candidate["item_ref"]: candidate for candidate in row["request"]["constraints"]["tidy_candidates"]}
    try:
        result = json.loads(text)
        if not isinstance(result, dict) or set(result) != {"suggestions"} or not isinstance(result["suggestions"], list):
            raise ValueError("invalid tidy shape")
        suggestions = {}
        for proposal in result["suggestions"]:
            if (not isinstance(proposal, dict) or set(proposal) != {"item_ref", *_FIELDS}
                    or not isinstance(proposal["item_ref"], str) or proposal["item_ref"] not in candidates
                    or proposal["item_ref"] in suggestions):
                raise ValueError("invalid tidy reference")
            fields = {}
            for key in _FIELDS:
                value = proposal[key]
                if value is not None:
                    if not isinstance(value, str) or not value.strip() or len(value) > 256 or not value.isprintable():
                        raise ValueError("invalid tidy field")
                    fields[key] = value.strip()
            ref = proposal["item_ref"]
            suggestions[ref] = (candidates[ref]["record_sha256"], fields)
        if set(suggestions) != set(candidates):
            raise ValueError("missing tidy reference")
        return suggestions
    except (ValueError, TypeError):
        logger.warning("Invalid pre-review tidy result; no suggestions shown")
        return {}


def _added_day(now):
    """Added batches belong to the open 06:00-to-06:00 card, not midnight."""
    return (now.astimezone(SINGAPORE) - timedelta(hours=6)).date()


def run_added_tidy(context, now, *, batch_id, item_refs, enabled=False):
    """Queue one proposal request per already-saved card batch, at any hour.

    The caller authorizes current Brain visibility before supplying the context.
    This returns after the existing GT outbox reserves the request; items stay
    answerable while the worker delivers it and Diablo returns proposals.
    """
    if not enabled or not context.settings.enabled:
        return {"outcome": "disabled"}
    if context.why_log is None:
        raise RuntimeError("review auto tidy requires the review why-log")
    if (not isinstance(batch_id, str) or re.fullmatch(r"[a-z0-9][a-z0-9-]{0,127}", batch_id) is None
            or not isinstance(item_refs, (tuple, list)) or not 1 <= len(item_refs) <= 20
            or any(not isinstance(ref, str) or not ref or len(ref) > 128 for ref in item_refs)
            or len(set(item_refs)) != len(item_refs)):
        raise ValueError("auto tidy needs the exact saved batch")
    day, log = _added_day(now), context.why_log
    key = _ADDED_BATCH + batch_id
    frozen = next((row for row in log.name_requests(day) if row["item_ref"] == key), None)
    if frozen is not None and sorted(frozen["request"]["constraints"]["saved_item_refs"]) != sorted(item_refs):
        raise ValueError("auto tidy retry names a different batch")
    if frozen is None:
        inputs = collect_morning_inputs(context.sources, log.morning_history(), now, ranking=context.ranking,
            previous_review_at=log.last_sent_review_at(), bootstrap=log.morning_bootstrap(),
            shown_item_refs=log.shown_item_refs())
        if inputs.source_status.get("brain") is not None:
            raise SourceUnavailable("brain items unavailable for auto tidy")
        candidates = [asdict(candidate) for candidate in list_tidy_candidates(inputs) if candidate.item_ref in item_refs]
        if not candidates:
            return {"outcome": "empty", "count": 0}
        request = dict(sender_participant=context.settings.sender_participant,
            delivery_participant=context.settings.delivery_participant, project=context.settings.project or "",
            client_operation_id=f"added-tidy-{day.isoformat()}-{batch_id}",
            objective="Tidy the things just added to the morning review",
            text="Use only the original words and context in constraints.tidy_candidates.",
            constraints={"tidy_candidates": candidates, "saved_item_refs": sorted(item_refs)},
            next_action="Suggest a short action title and one label for each thing. For a project-like thing, also suggest "
                "one short name. Use null when its existing words are already clear. Do not invent facts, dates or steps. "
                "These are proposals: save nothing to Brain Home and post nothing to Nic. Return only the requested JSON.",
            expected_reply='JSON {"suggestions":[{"item_ref":"the supplied reference","suggested_title":null,'
                '"suggested_label":null,"suggested_name":null}]} with every supplied reference once.')
        _bound(request)
        digest = hashlib.sha256(json.dumps(candidates, sort_keys=True).encode()).hexdigest()
        frozen = log.prepare_name_request(day, key, digest, now, request)
    if frozen["request_id"] is not None:
        return {"outcome": "already_requested"}
    receipt = deliver(context.service, **frozen["request"])
    if receipt.error is not None:
        raise GremlinTownError(receipt.error.code, "The added-things tidy request was not delivered")
    log.record_name_request(day, key, receipt.request_id)
    return {"outcome": "requested", "count": len(frozen["request"]["constraints"]["tidy_candidates"]), "sent": 1}


def read_added_tidy(context, now, *, current_items, open_item_refs, enabled=False, include_waiting=False):
    """Only unchanged, still-answerable items receive asynchronous proposals.

    current_items and open_item_refs must come from the same fresh, policy-checked
    card redraw. An answer or rename suppresses the old proposal permanently for
    that version. No proposal is itself a Brain write or a review answer.
    """
    if not enabled or not context.settings.enabled or context.why_log is None:
        return {"suggestions": {}, "waiting_refs": []} if include_waiting else {}
    day = _added_day(now)
    opened = datetime.combine(day, time(6), SINGAPORE)
    expires = opened + timedelta(days=1)
    from brain_core import work
    index = work.index_things(entry.record for entry in current_items)
    raw = {entry.record.id: entry.record for entry in current_items}
    current = {ref: raw[record.id].sha256 for ref, record in index.items()
               if ref in open_item_refs and record.state == "open"}
    output, waiting = {}, set()
    for row in context.why_log.name_requests(day):
        if not row["item_ref"].startswith(_ADDED_BATCH):
            continue
        candidates = {entry["item_ref"]: entry for entry in row["request"]["constraints"]["tidy_candidates"]}
        eligible = {ref for ref, entry in candidates.items() if current.get(ref) == entry["record_sha256"]}
        if not row["request_id"]:
            waiting.update(eligible)
            continue
        try:
            result = context.service.local_request_result(Actor(row["request"]["sender_participant"]), row["request_id"])
            if result is None:
                waiting.update(eligible)
                continue
            text, returned_at = result
            if not opened <= datetime.fromisoformat(row["prepared_at"]) <= returned_at <= now < expires:
                continue
            def unique(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("duplicate field")
                    value[key] = item
                return value
            value = json.loads(text, object_pairs_hook=unique)
            if not isinstance(value, dict) or set(value) != {"suggestions"} or not isinstance(value["suggestions"], list):
                raise ValueError("invalid tidy shape")
            proposals = {}
            for proposal in value["suggestions"]:
                if (not isinstance(proposal, dict) or set(proposal) != {"item_ref", *_FIELDS}
                        or not isinstance(proposal["item_ref"], str) or proposal["item_ref"] not in candidates
                        or proposal["item_ref"] in proposals):
                    raise ValueError("invalid tidy reference")
                fields = {}
                for key in _FIELDS:
                    value = proposal[key]
                    if value is not None:
                        if not isinstance(value, str) or not value.strip() or len(value) > 256 or not value.isprintable():
                            raise ValueError("invalid tidy field")
                        fields[key] = value.strip()
                ref = proposal["item_ref"]
                proposals[ref] = (candidates[ref]["record_sha256"], fields)
            if set(proposals) != set(candidates):
                raise ValueError("missing tidy reference")
            output.update({ref: suggestion for ref, suggestion in proposals.items() if current.get(ref) == suggestion[0]})
        except (GremlinTownError, OSError, sqlite3.Error, ValueError, TypeError):
            logger.warning("Added-things tidy result unavailable or invalid; items remain answerable")
    return {"suggestions": output, "waiting_refs": sorted(waiting - output.keys())} if include_waiting else output


def added_tidy_revision(context, now, *, enabled=False):
    """Local GT fingerprint for polling; a change still needs a fresh card read."""
    if not enabled or not context.settings.enabled or context.why_log is None:
        return "disabled"
    values = []
    for row in context.why_log.name_requests(_added_day(now)):
        if not row["item_ref"].startswith(_ADDED_BATCH):
            continue
        state = None
        if row["request_id"]:
            page = context.service.requests_list(Actor(row["request"]["sender_participant"]),
                {"request_id": row["request_id"], "include_recovery_held": True, "limit": 1})
            if len(page.items) != 1:
                raise SourceUnavailable("added-things tidy request unavailable")
            request = page.items[0]
            state = [request["revision"], request["work_state"], request["recovery_hold"]]
        values.append([row["item_ref"], row["request_id"], state])
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
