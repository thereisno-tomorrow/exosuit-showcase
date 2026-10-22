"""Compile only the action bound to an offered card button."""
from dataclasses import asdict
from datetime import date, datetime
from copy import deepcopy
from zoneinfo import ZoneInfo
from brain_core.contracts import canonical_json

from .capture import capture_parts
from gremlin_town.mcp.brain_runtime import WorkRequest
from .models import CardError


def words(fields, *, limit=1000):
    value = fields.get("value") if isinstance(fields, dict) else None
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\0" in value:
        raise CardError("Write your answer in the box, then try again.")
    return value


def planning_instant(value, zone_name):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            zone = ZoneInfo(zone_name)
            early, late = parsed.replace(tzinfo=zone, fold=0), parsed.replace(tzinfo=zone, fold=1)
            if early.utcoffset() != late.utcoffset():
                raise CardError("That local time is repeated or skipped by daylight saving. Include the intended UTC offset, such as +11:00.")
            parsed = early
        return parsed.isoformat()
    except CardError:
        raise
    except (ValueError, TypeError):
        raise CardError("Use a date and time in YYYY-MM-DD HH:MM, in the shown zone. Nothing changed.") from None


def make_request(operation, spec, fields, day):
    action = spec["action"]
    ref = spec.get("ref")
    target = {"thing": ref} if ref else {}
    answers = (spec["line"],) if spec.get("line") else ()
    expected = {ref: spec["expected"]} if ref else None
    if spec.get("context_expected"):
        expected = {**(expected or {}), **spec["context_expected"]}
    optional_notes = {'progress': 'Made progress', 'arrival': 'Required reply or delivery arrived', 'dated_outcome': 'Recorded actual result', 'appointment': 'Moved appointment', 'portion_result': 'Recorded completed quantity', 'portion_select': 'Choose ready parts for today', 'timebox': 'Set a bounded work session'}
    if action in optional_notes and not fields.get('value', '').strip():
        fields = {**fields, 'value': optional_notes[action]}
    value = spec.get("value")
    message = spec["label"]
    if action == "followup_disposition":
        disposition = deepcopy(spec["disposition"])
        moves = [{"move": "organise", **target, "followup_disposition": disposition}]
        expected = deepcopy(spec["disposition_expected"])
        message = "Approve required return change\n" + canonical_json(disposition)
        answers = ()
    elif action == "add":
        message = words(fields)
        parts = spec.get("capture_parts") or capture_parts(message, fields.get("order", "independent"))
        moves = []
        previous = None
        for number, part in enumerate(parts, 1):
            existing = part.get("existing_ref")
            move = {"move": "organise", "thing": existing, "note": message} if existing else {"move": "new", "title": part["title"]}
            if part.get("then"):
                if existing:
                    move["requirements"] = {"remove": [], "add": [previous]}
                else:
                    move["after"] = [previous]
            moves.append(move)
            previous = existing or f"#{number}"
        message += "".join("\n" + canonical_json(move["requirements"]) for move in moves if move.get("requirements"))
        expected = spec.get("capture_expected") or None
        answers = ()
    elif action == "undo_drop":
        answers = ()
        message = "Restore recent drop"
        moves = [{"move": "undo", "change": spec["change"]}]
    elif action == "note":
        answers = ()
        message = words(fields)
        moves = [{"move": "organise", **target, "note": message}]
    elif action == "delegate":
        answers = ()
        who = words({"value": fields.get("who")}, limit=128)
        note = words(fields)
        message = who + " · " + note
        moves = [{"move": "wait", **target, "kind": "theirs", "who": who}, {"move": "organise", **target, "note": note}]
    elif action == "dated_outcome":
        answers = ()
        message = words(fields)
        try:
            actual_day = date.fromisoformat(fields.get("day", ""))
            if actual_day > day:
                raise ValueError
        except ValueError:
            raise CardError("Choose the actual past or current calendar date.") from None
        outcome = fields.get("kind", "").strip().casefold()
        message += " · " + actual_day.isoformat()
        if outcome == "finished":
            moves = [{"move": "tick", **target, "when": actual_day.isoformat()}]
        elif outcome in {"progress", "not_done"}:
            moves = [{"move": "organise", **target, "review_outcome": outcome, "review_day": actual_day.isoformat(), "note": words(fields)}]
        else:
            raise CardError("Choose Finished, Made progress, or Didn't do it. Nothing changed.")
    elif action == "appointment":
        answers = ()
        value = planning_instant(fields.get("when", ""), spec.get("timezone", "Asia/Singapore"))
        mode = fields.get("mode", "").strip().casefold()
        if mode not in {"confirmed", "tentative"}:
            raise CardError("Choose confirmed or tentative for the new appointment.")
        zone = spec.get("timezone", "Asia/Singapore")
        message = words(fields) + " · " + value + " (" + zone + ")"
        moves = [{"move": "date", **target, "when": value, "appointment_timezone": zone, "kind": "appointment", "proposed": mode == "tentative"}]
    elif action == "under":
        answers = ()
        message = "Put under " + spec["parent_title"]
        moves = [{"move": "organise", **target, "under": spec["parent_ref"]}]
        expected[spec["parent_ref"]] = spec["parent_expected"]
    elif action == "classify":
        answers = ()
        kind = fields.get("material_kind")
        if kind not in {"information", "reflection", "idea", "arrangement"}:
            raise CardError("Choose how to keep this note.")
        message = "Keep as " + kind
        moves = [{"move": "organise", **target, "material_kind": kind}]
    elif action == "requirement":
        answers = ()
        requirements = spec["requirements"]
        message = fields.get("note", "") + "\n" + canonical_json(requirements)
        moves = [{"move": "organise", **target, "requirements": requirements}]
    elif action in {"portion_result", "portion_select"}:
        answers = ()
        message = words(fields)
        if action == "portion_select":
            value = {"day": day.isoformat(), "ids": list(dict.fromkeys(fields["ids"] if isinstance(fields.get("ids"), list) else [value.strip() for value in fields.get("ids", "").split(",") if value.strip()]))}
            key = "portion_selection"
        else:
            try:
                quantity = int(fields.get("done_quantity", ""))
            except ValueError:
                raise CardError("Enter how many of this part you have finished in total, including earlier work.") from None
            value = [{"id": fields.get("id", ""), "done_quantity": quantity, "text": message}]
            if fields.get("correct", "").strip().casefold() == "yes":
                value[0]["correct"] = True
            key = "portion_outcomes"
        message += "\n" + canonical_json(value)
        moves = [{"move": "organise", **target, key: value}]
        if action == "portion_result":
            moves[0]["review_day"] = spec.get("outcome_day") or day.isoformat()
    elif action in {"timebox", "hold"}:
        answers = ()
        message = words(fields)
        planning = deepcopy(spec.get("planning", {})) if action == "timebox" else {}
        if action == "timebox":
            try:
                minutes = int(fields.get("minutes", ""))
                if minutes <= 0:
                    raise ValueError
            except ValueError:
                raise CardError("Enter the number of minutes for this session, such as 30.") from None
            planning["work_sessions"] = [s for s in planning.get("work_sessions", ()) if s["day"] != day.isoformat()] + [{"day": day.isoformat(), "minutes": minutes, "kind": "timebox"}]
            message += "\n" + canonical_json(planning)
            moves = [{"move": "organise", **target, "planning": planning}]
        else:
            planning["held_time"] = [{"start": planning_instant(fields.get("start"), spec.get("timezone", "Asia/Singapore")), "end": planning_instant(fields.get("end"), spec.get("timezone", "Asia/Singapore"))}]
            message = "Held time · " + message + "\n" + canonical_json(planning) + "\narrangement"
            moves = [{"move": "new", "title": "Held time · " + words(fields)}, {"move": "organise", "thing": "#1", "material_kind": "arrangement", "planning": planning}]
            expected = None
    elif action == "planning_cancel":
        answers = ()
        planning = deepcopy(spec.get("planning", {}))
        planned_operation = planning.get("operation")
        if not planned_operation:
            raise CardError("There is no agreed start and follow-up to cancel. Nothing changed.")
        if planned_operation.get("actual_start"):
            raise CardError("This work has already started, so its follow-up still needs doing. Open the follow-up if someone else will take it over.")
        planning.pop("operation")
        message = "Cancel before startup · " + canonical_json(planning)
        moves = [{"move": "organise", **target, "planning": planning}]
    elif action.startswith("planning_"):
        answers = ()
        planning = deepcopy(spec.get("planning", {}))
        def field(key, limit=500):
            return words({"value": fields.get(key)}, limit=limit).strip()
        def instant(value):
            return planning_instant(value, spec.get("timezone", "Asia/Singapore"))
        try:
            if action == "planning_resource":
                value = dict(resource=field("resource"), start=instant(field("start")), end=instant(field("end")))
                planning["resources"] = [r for r in planning.get("resources", ()) if r["resource"] != value["resource"]] + [value]
            elif action == "planning_portion":
                ready = field("ready").casefold()
                if ready not in {"yes", "no"}:
                    raise CardError("Enter yes if this part can be done now, or no if it is still blocked.")
                value = dict(id=field("id"), quantity=int(field("quantity")), ready=ready == "yes", checks=[s.strip() for s in fields.get("checks", "").splitlines() if s.strip()])
                if fields.get("window", "").strip():
                    start, end = fields["window"].split(" / ", 1)
                    value.update(window_start=instant(start), window_end=instant(end))
                prior = next((p for p in planning.get("portions", ()) if p["id"] == value["id"]), {})
                if "done_quantity" in prior:
                    value["done_quantity"] = prior["done_quantity"]
                planning["portions"] = [p for p in planning.get("portions", ()) if p["id"] != value["id"]] + [value]
            elif action == "planning_window":
                planning["windows"] = [dict(start=instant(field("start")), end=instant(field("end")))]
            elif action == "planning_shared":
                value = dict(id=field("id"), kind=field("kind").casefold(), minutes=int(field("minutes")), task_refs=spec["task_refs"])
                planning["shared_blocks"] = [b for b in planning.get("shared_blocks", ()) if b["id"] != value["id"]] + [value]
            elif action == "planning_operation":
                field("note")
                planning["operation"] = dict(followup_ref=spec["followup_ref"], delay_minutes=int(field("delay")), approved=True)
            else:
                field("note")
                if not planning.get("operation", {}).get("approved"):
                    raise CardError("Agree to the work and its follow-up before recording when it started.")
                planning["operation"]["actual_start"] = instant(field("start"))
            from brain_core.planning import validate_planning
            validate_planning(planning)
        except (ValueError, KeyError):
            raise CardError("Check the amounts, dates and follow-up details. Nothing changed.") from None
        message = "\n".join(str(value) for value in fields.values()) + "\n" + canonical_json(planning)
        moves = [{"move": "organise", **target, "planning": planning}]
    elif action == "correction":
        answers = ()
        message = words(fields)
        moves = [{"move": "organise", **target, "reopen": True, "note": message}]
    elif action in {"canceled", "no_show"}:
        answers = ()
        field = "occurrence_outcome" if spec.get("scope") == "occurrence" else "review_outcome"
        value = "missed" if action == "no_show" and field == "occurrence_outcome" else action
        moves = [{"move": "organise", **target, field: value, "review_day": spec["outcome_day"]}]
    elif action in {"progress", "not_done"}:
        answers = ()
        message = words(fields) if action == "progress" else "Didn't do it"
        moves = [{"move": "organise", **target, "review_outcome": action if action == "progress" else "not_done",
                  "review_day": spec["outcome_day"]}]
        if action == "not_done" and spec.get("scope") in {"appointment", "occurrence"}:
            moves[0].pop("review_outcome")
            moves[0]["occurrence_outcome" if spec["scope"] == "occurrence" else "review_outcome"] = "missed" if spec["scope"] == "occurrence" else "not_happened"
        if action == "progress":
            moves[0]["note"] = message
            if fields.get("remaining", "").strip():
                try:
                    moves[0]["remaining_minutes"] = int(fields["remaining"])
                except ValueError:
                    raise CardError("Enter how many minutes remain, such as 15. Use a whole number above zero.") from None
                message += " · " + fields["remaining"] + " min remaining"
    elif action == "finished":
        answers = ()
        message += " · " + (spec.get("outcome_day") or day.isoformat())
        if spec.get("scope") == "occurrence":
            moves = [{"move": "organise", **target, "occurrence_outcome": "done", "review_day": spec["outcome_day"]}]
        elif spec.get("scope") == "appointment":
            moves = [{"move": "organise", **target, "review_outcome": "happened", "review_day": spec["outcome_day"]}]
        else:
            moves = [{"move": "tick", **target, "when": spec.get("outcome_day") or day.isoformat()}]
    elif action == "wait":
        answers = ()
        message = words(fields)
        moves = [{"move": "wait", **target, "kind": "needs", "who": words({"value": fields.get("who")}, limit=128), "what": message}]
        if fields.get('expected', '').strip():
            moves[0]['expected'] = fields['expected'].strip()
            message += ' · expected ' + fields['expected'].strip()
        message = fields["who"] + " · " + message
    elif action == "arrival":
        answers = ()
        message = words(fields)
        moves = [{"move": "organise", **target, "clear_wait": True, "note": message}]
    elif action == "done":
        moves = [{"move": "tick", **target}]
    elif action == "drop" and spec.get("scope") in {"appointment", "occurrence"}:
        answers = ()
        moves = [{"move": "organise", **target, "occurrence_outcome" if spec["scope"] == "occurrence" else "review_outcome": "canceled", "review_day": spec["outcome_day"]}]
    elif action == "drop":
        if spec.get("whole_item") is True:
            answers = ()
        message = "Drop"
        moves = [{"move": "end", **target, "ending": "dropped"}]
    elif action == "save":
        message = "Save"
        move = {"move": "organise", **target}
        for key in ("title", "label"):
            if spec.get("suggested_" + key):
                move[key] = spec["suggested_" + key]
        if len(move) == 2:
            move["accept"] = True
        moves = [move]
    elif action in {"rename", "next", "parts", "change"}:
        message = words(fields, limit=1000 if action == "parts" else 256)
        if action == "parts":
            order = fields.get("order")
            if isinstance(order, list) and len(order) == 1:
                order = order[0]
            if order not in {"ordered", "independent"}:
                raise CardError("Choose ordered steps or independent steps. Nothing changed.")
            titles = [line.strip() for line in message.splitlines() if line.strip()]
            if not 1 <= len(titles) <= 20 or any(len(title) > 256 for title in titles):
                raise CardError("Enter up to 20 steps, one per line, with each title at most 256 characters.")
            parts = [{"title": title, **({"then": True} if order == "ordered" and i else {})}
                     for i, title in enumerate(titles)]
            message += "\n" + ("Ordered steps: each new step waits for the preceding new step." if order == "ordered" else "Independent steps: no new dependencies.")
            move = {"move": "parts", **target, "parts": parts}
        elif action == "next":
            move = {"move": "next_action", **target, "text": message}
        else:
            move = {"move": "organise", **target, "title": message}
        moves = [move]
    elif action == "remove_plan":
        message = "Remove this planned date; keep the task open and available."
        moves = [{"move": "organise", **target, "can_do_now": True}]
        answers = ()
    elif action in {"length", "length_custom"}:
        if action == "length_custom":
            raw = fields.get("minutes", "")
            if not isinstance(raw, str) or not raw.strip().isdigit() or int(raw) <= 0:
                raise CardError("Enter a positive whole number of minutes. Your answer is kept; nothing changed.")
            value = int(raw)
        message = f"{value} min"
        moves = [{"move": "organise", **target, "rough_minutes": value, "length_is_nics": True}]
    elif action in {"date", "park"}:
        if value == "picker":
            try:
                week = date.fromisoformat(fields["week"])
                offset = int(fields["day"])
                if week.weekday() != 0 or not 0 <= offset <= 6:
                    raise ValueError
                from datetime import timedelta
                value = (week + timedelta(days=offset)).isoformat()
            except (ValueError, TypeError, KeyError):
                raise CardError("Choose both a week and a day.") from None
        if value != "Someday":
            try:
                chosen = date.fromisoformat(value)
                if chosen < day or (chosen - day).days > 180:
                    raise ValueError
            except (ValueError, TypeError):
                raise CardError("That day has passed or is too far away. Pick another.") from None
        if action == "park":
            message = "Park until " + value
            moves = [{"move": "park", **target, **({"event": value} if value == "Someday" else {"until": value})}]
        else:
            kind = spec.get("mode", "planned")
            phrase = value + (" at " + spec["at"] if kind == "appointment" and spec.get("at") else "")
            message = f"For {phrase}"
            moves = [{"move": "date", **target, "when": phrase, "kind": kind}]
            if kind == "planned":
                moves[0]["unpark"] = True
    else:
        raise CardError("That answer is not available on this screen.")
    return WorkRequest(operation, message, "owner", "direct", tuple(moves), answers=answers, expected_records=expected)


def request_data(request):
    return asdict(request)


def restore_request(data):
    return WorkRequest(**dict(data, moves=tuple(data["moves"]), answers=tuple(data.get("answers", ()))))
