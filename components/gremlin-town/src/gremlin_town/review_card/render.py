"""Discord Components V2, following the approved quiet, one-item mock."""
from copy import deepcopy
from datetime import date, datetime, timedelta
import hashlib
import re
from zoneinfo import ZoneInfo

from .models import CardError, CardRenderError
from .selectors import has_ready

QUIET, GOLD, RED, WHITE = 0x4A463F, 0xC99A3A, 0xFF5A63, 0xE9E6DF
COLOURS = ("plum", "amber", "sand", "clay", "sage", "teal", "stone")


def plain(value, limit=300):
    value = str(value or "").replace("\n", " ")[:limit]
    return re.sub(r"([\\`*_~|>])", r"\\\1", value).replace("@", "@\u200b")


def text(value):
    return {"type": 10, "content": value}


def gap(size=1):
    return {"type": 14, "divider": False, "spacing": size}


def row(*buttons):
    return {"type": 1, "components": list(buttons)}


def container(body, colour=QUIET):
    return {"type": 17, "components": body, "accent_color": colour}


def walk(components):
    for value in components:
        yield value
        yield from walk(value.get("components", []))
        for field in ("component", "accessory"):
            if field in value:
                yield from walk([value[field]])


def payload(components):
    if len(list(walk(components))) > 40:
        raise CardRenderError("This screen is too large to show. Open My day.")
    if sum(len(v.get("content", "")) for v in walk(components)) > 4000:
        raise CardRenderError("This screen is too long to show. Open My day.")
    return {"flags": 1 << 15, "allowed_mentions": {"parse": []}, "components": components}


def schedule_text(state, data):
    """Presentation only: stale placements never enter the planning projection."""
    status = data.get('schedule', {})
    available = bool(status.get('available'))
    cached = state.get('last_schedule', {})
    if available:
        slots = [slot for slot in data.get('timeline', ()) if slot.get('type') == 'task']
        prefix = "Latest schedule"
        stamp = status.get('fetched_at')
    else:
        slots = cached.get('timeline', ()) if cached.get('day', state['day']) == state['day'] else ()
        prefix = "Schedule unavailable. Last known placements; check again before relying on these times." if slots else "Schedule unavailable. Your saved choices are unchanged."
        stamp = cached.get('fetched_at') if cached.get('day', state['day']) == state['day'] else None
    lines = [prefix]
    if stamp:
        try:
            checked = datetime.fromisoformat(stamp).astimezone(ZoneInfo(state.get('timezone', 'Asia/Singapore')))
            lines.append(('Checked ' if available else 'Last known read ') + checked.strftime('%Y-%m-%d %H:%M %Z'))
        except (ValueError, TypeError):
            lines.append('Read time unavailable')
    zone = ZoneInfo(state.get('timezone', 'Asia/Singapore'))
    for slot in slots:
        start = datetime.fromisoformat(slot['start']).astimezone(zone)
        end = datetime.fromisoformat(slot['end']).astimezone(zone)
        lines.append(start.strftime('%d %b %H:%M') + ' to ' + end.strftime('%d %b %H:%M %Z') + ' - ' + str(slot['title']))
    if available and not slots:
        lines.append('No placements returned by the confirmed schedule read. Saved choices may still be awaiting placement.')
    if available:
        lines.append('Times can shift during the day.')
    return '\n'.join(lines)


def full_text(value):
    """Escape display text without silently shortening approval wording."""
    return re.sub(r"([\\`*_~|>])", r"\\\1", str(value or "")).replace("@", "@\u200b")


def text_pages(value, size=1500):
    # Split raw text first so an escape sequence is never cut between pages.
    return [full_text(value[i:i + size]) for i in range(0, len(value), size)] or [""]


def step_context(view, ref):
    index = view.get("index", {})
    children = sorted((r for r in index.values() if r.data.get("parent") == ref),
                      key=lambda r: (r.data.get("order", 0), r.title, r.id))
    sections = []
    for number, child in enumerate(children, 1):
        blockers = [index.get(target) for target in child.data.get("after", ())]
        dependency = "; ".join((b.title + " (" + b.state + ")") if b else "Unavailable prerequisite"
                               for b in blockers)
        sections.append(f"{number}. {child.title} · {child.state}\n" + str(child.body or "") +
                        ("\nRequires: " + dependency + ". Only completion satisfies a prerequisite." if blockers else
                         "\nIndependent of earlier steps."))
    return "\n\n".join(sections) or "No existing steps."


def day_label(day):
    return f"{day:%a} {day.day} {day:%b}"


def appointment_label(card, zone_name):
    appointment = card.get("dates", {}).get("appointment", {})
    if not appointment.get("day") or not appointment.get("at"):
        return None
    from brain_core.timewords import SINGAPORE
    instant = datetime.fromisoformat(appointment["day"] + "T" + appointment["at"]).replace(tzinfo=SINGAPORE)
    local = instant.astimezone(ZoneInfo(zone_name))
    return ("Tentative appointment" if appointment.get("confirmed") is False else "Appointment") + " · " + local.strftime("%d %b %H:%M") + " · " + zone_name


def _modal(token, spec, day):
    if spec['action'] == 'return_reason':
        body = [{'type':18, 'label':'Why is the required return changing?', 'component':
            {'type':4,'custom_id':'reason','style':2,'required':True,'max_length':1024}}]
        if spec['kind'] == 'transfer':
            body += [{'type':18,'label':'Who has agreed to do the replacement?', 'component':
                {'type':4,'custom_id':'assignee','style':1,'required':True,'max_length':128}},
                {'type':18,'label':'Confirm agreed responsibility', 'description':'Naming someone does not notify them or prove delivery.', 'component':
                {'type':3,'custom_id':'agreed','required':True,'min_values':1,'max_values':1,'options':[
                    {'label':'This person has agreed','value':'agreed'},
                    {'label':'Responsibility is not agreed','value':'not_agreed'}]}}]
        return {'type':9,'data':{'custom_id':token,'title':'Explain the return change','components':body}}
    if spec["action"] == "classify":
        return {"type": 9, "data": {"custom_id": token, "title": "Keep this note", "components": [
            {"type": 18, "label": "What is this?", "component": {"type": 3, "custom_id": "material_kind", "required": True,
                "min_values": 1, "max_values": 1, "options": [{"label": name.title(), "value": name, "description": description} for name, description in (("information", "Something useful to remember."), ("reflection", "Something you noticed or learned."), ("idea", "A possibility to think about later."), ("arrangement", "An agreed plan, meeting, or event."))]}}]}}
    if spec["action"] in {"requirement", "portion_result", "portion_select", "timebox", "hold", "dated_outcome", "appointment"}:
        schemas = {
            "requirement": ("Change an earlier step", (("item", "Which work is affected?", True), ("remove", "Earlier step no longer required", True), ("add", "New earlier step (optional)", False), ("note", "What changed?", True))),
            "portion_result": ("Update a part or batch", (("id", "Saved part or batch name", True), ("done_quantity", "Total quantity completed so far", True), ("value", "What happened to this part or batch?", True), ("correct", "Correct earlier finished quantity? yes or no", False))),
            "portion_select": ("Choose ready parts or batches", (("item", "Item title (only when choosing from the day)", False), ("ids", "Ready names, separated by commas", True), ("value", "Which named parts will you do today?", True))),
            "timebox": ("Set a work session", (("item", "Which work? Copy its saved title", True), ("minutes", "Minutes for this session", True), ("value", "What will you work on in this session?", True))),
            "dated_outcome": ("Record actual result", (("kind", "finished, progress, or not_done", True), ("day", "Actual calendar date (YYYY-MM-DD)", True), ("value", "What happened?", True))),
            "appointment": ("Move appointment", (("when", "New date and time", True), ("mode", "confirmed or tentative", True), ("value", "What changed?", True))),
            "hold": ("Keep time free", (("start", "Local start (date and time)", True), ("end", "Local end (date and time)", True), ("value", "What is this time reserved for?", True))),
        }
        title, fields = schemas[spec["action"]]
        body = [{"type": 18, "label": label, "description": form_example(key, day), "component": {"type": 4, "custom_id": key, "required": required, "style": 1, "max_length": 500}} for key,label,required in fields]
        if spec["action"] == "dated_outcome":
            body[0] = {"type": 18, "label": "What happened?", "component": {"type": 3, "custom_id": "kind", "required": True,
                "min_values": 1, "max_values": 1, "options": [{"label": label, "value": value} for label, value in
                    (("Finished", "finished"), ("Made progress", "progress"), ("Didn't do it", "not_done"))]}}
        return {"type": 9, "data": {"custom_id": token, "title": title, "components": body}}
    if spec["action"].startswith("planning_"):
        schemas = {
            "planning_resource": ("Book shared equipment", (("resource", "Equipment or space only one job can use", True), ("start", "Start (date and time)", True), ("end", "End (date and time)", True))),
            "planning_portion": ("Add a part or batch", (("id", "Part or batch name", True), ("quantity", "Quantity (whole number)", True), ("ready", "Ready now? yes or no", True), ("checks", "Confirmed checks, one per line", False), ("window", "Optional window: start / end", False))),
            "planning_operation": ("Agree the start and follow-up", (("followup", "Exact existing follow-up title", True), ("delay", "Minutes after the work actually starts", True), ("note", "What start and follow-up are you agreeing to?", True))),
            "planning_window": ("When this work can happen", (("start", "Available from (date and time)", True), ("end", "Available until (date and time)", True))),
            "planning_shared": ("Shared travel or setup", (("id", "Name this shared block", True), ("kind", "travel or setup", True), ("minutes", "Minutes for the shared block", True), ("tasks", "Saved task titles, separated by commas", True))),
            "planning_start": ("Record when it started", (("start", "When it actually started (date and time)", True), ("note", "What started?", True))),
        }
        title, fields = schemas[spec["action"]]
        body = [{"type": 18, "label": label, "description": form_example(key, day), "component": {"type": 4, "custom_id": key,
                "required": required, "style": 2 if key in {"checks", "note"} else 1, "max_length": 500}}
                for key, label, required in fields]
        return {"type": 9, "data": {"custom_id": token, "title": title, "components": body}}
    if spec.get("value") == "picker":
        monday = day - timedelta(days=day.weekday())
        weeks = [monday + timedelta(weeks=i) for i in range(25)]
        body = [
            {"type": 18, "label": "Week", "component": {"type": 3, "custom_id": "week", "required": True,
                "min_values": 1, "max_values": 1, "options": [
                    {"label": "This week" if i == 0 else "Week of " + day_label(value), "value": value.isoformat()}
                    for i, value in enumerate(weeks)]}},
            {"type": 18, "label": "Day", "component": {"type": 21, "custom_id": "day", "required": True,
                "options": [{"label": name, "value": str(i)} for i, name in enumerate(("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"))]}},
        ]
        title = "Pick a date"
    else:
        action = spec["action"]
        title, label = {"add": ("Add things", "What's new?"), "parts": ("Type the steps…", "What are the steps?"),
            "next": ("What's the next step?", "What's the next step?"), "rename": ("Change name", "New name"),
            "change": ("Change", "What should it say?"), "under": ("Add to a project", "Existing project title"), "duplicate_choose": ("Choose an existing item", "Existing item reference"),
            "progress": ("Made progress", "What did you get done?"), "wait": ("Waiting", "What do you need from them?"),
            "inspect": ("Update an item", "Which item? Copy its saved title"), "correction": ("Correct completed work", "What is wrong or still needed?"), "arrival": ("Arrived", "What arrived or was confirmed?"), "delegate": ("Hand off this work", "What is agreed, and what will you still do?"), "note": ("Something else", "What happened or is still unclear?"), "capacity": ("Set available hours", "Hours left after appointments and commitments"), "timezone": ("Local time zone", "City or time zone, e.g. Sydney"),
            "ask_words": ("Ask Diablo", "What should Diablo do?")}[action]
        field = {"type": 4, "custom_id": "value", "required": True, "style": 2 if action in {"add", "parts", "ask_words"} else 1,
                 "max_length": 1000 if action in {"add", "parts", "ask_words"} else 256}
        labelpart = {"type": 18, "label": label, "component": field}
        if action == "add":
            labelpart["description"] = "One item per line. Keep commas in titles. Choose ordered or independent work below."
        if action == "next":
            labelpart["description"] = "The very next physical thing you'd do"
        body = [labelpart]
        if action == "add":
            body.append({"type":18,"label":"How are these items related?","component":
                {"type":3,"custom_id":"order","required":True,"min_values":1,"max_values":1,"options":[
                    {"label":"Independent: no new dependencies","value":"independent","default":True},
                    {"label":"Ordered: each waits for the previous item","value":"ordered"}]}})
        if action == "parts":
            labelpart["description"] = "One step per line. Commas stay inside steps. Existing steps and requirements are kept."
            body.append({"type": 18, "label": "Do these new steps depend on each other?", "component":
                {"type": 3, "custom_id": "order", "required": True, "min_values": 1, "max_values": 1,
                 "options": [{"label": "Ordered: each waits for the previous new step", "value": "ordered"},
                             {"label": "Independent: no new dependencies", "value": "independent"}]}})
        if action == "progress":
            body.append({"type": 18, "label": "About how many minutes remain? (optional)", "component":
                {"type": 4, "custom_id": "remaining", "required": False, "style": 1, "max_length": 7}})
        if action == "correction":
            body.insert(0, {"type": 18, "label": "Which completed item? Copy its saved title", "component":
                {"type": 4, "custom_id": "item", "required": True, "style": 1, "max_length": 256}})
        if action in {"wait", "delegate"}:
            body.insert(0, {"type": 18, "label": "Who is taking this over?" if action == "delegate" else "Who or what is missing?", "component":
                {"type": 4, "custom_id": "who", "required": True, "style": 1, "max_length": 128}})
    return {"type": 9, "data": {"custom_id": token, "title": title, "components": body}}


def modal(token, spec, day):
    if spec["action"] == "remove_plan":
        return {"type": 9, "data": {"custom_id": token, "title": "Remove planned date", "components": [
            {"type": 18, "label": "Optional note", "description": "This removes the planned date and keeps the task open and available. Focus is unchanged.",
             "component": {"type": 4, "custom_id": "note", "required": False, "style": 1, "max_length": 500}}]}}
    if spec["action"] == "length_custom":
        return {"type": 9, "data": {"custom_id": token, "title": "Whole-task estimate", "components": [
            {"type": 18, "label": "Minutes for the whole task", "description": "A positive whole number. Use a work session separately for a bounded attempt.",
             "component": {"type": 4, "custom_id": "minutes", "required": True, "style": 1, "max_length": 6}}]}}
    result = _modal(token, spec, day)
    selected = spec.get('selected_fields', {})
    body = [part for part in result['data']['components'] if part.get('component', {}).get('custom_id') not in selected]
    if spec['action'] == 'under' and selected:
        body = [{'type': 18, 'label': 'Note (optional)', 'component': {'type': 4, 'custom_id': 'note', 'required': False, 'style': 1, 'max_length': 500}}]
    typed = {'ready': [('Ready now', 'yes'), ('Not ready', 'no')],
             'correct': [('Keep earlier quantity', 'no'), ('Correct earlier quantity', 'yes')],
             'mode': [('Confirmed', 'confirmed'), ('Tentative', 'tentative')],
             'kind': [('Travel', 'travel'), ('Setup', 'setup')]}
    for part in body:
        field = part.get('component', {})
        key = field.get('custom_id')
        if key in typed and (key != 'kind' or spec['action'] == 'planning_shared'):
            part.pop('description', None)
            part['label'] = {'ready': 'Is this part ready?', 'correct': 'Is this a correction?', 'mode': 'Is the appointment confirmed?', 'kind': 'Shared block type'}[key]
            part['component'] = {'type': 3, 'custom_id': key, 'required': field.get('required', True),
                                 'min_values': 1, 'max_values': 1, 'options': [{'label': label, 'value': value} for label,value in typed[key]]}
        if key == 'value' and spec['action'] in {'progress', 'arrival', 'dated_outcome', 'appointment', 'portion_result', 'portion_select', 'timebox'}:
            field['required'] = False
            part['label'] = 'Note (optional)' if len(part['label']) + 11 > 45 else part['label'] + ' (optional)'
    if spec['action'] == 'wait':
        body.append({'type': 18, 'label': 'Expected or check date (optional)', 'description': 'E.g. ' + day.isoformat(),
                     'component': {'type': 4, 'custom_id': 'expected', 'required': False, 'style': 1, 'max_length': 100}})
    result['data']['components'] = body
    return result


def drop_label(line, detail):
    scope = line.get("scope") or detail.get("scope") or "task"
    return {"subtask": "Drop this step", "step": "Drop this step", "project": "Drop this project",
            "goal": "Drop this goal", "appointment": "Cancel this appointment", "occurrence": "Cancel this occurrence"}.get(scope, "Drop this task")


def form_example(key, day=None):
    day = day or date.today()
    examples = {"start": "E.g. 2026-10-05 09:00 in your selected time zone.", "end": "E.g. 2026-10-05 10:00 in your selected time zone.",
            "when": "E.g. 2026-10-05 09:30.", "day": "E.g. 2026-10-04. Use the day it happened.",
            "item": "Copy the title as it appears in your saved work.", "followup": "Copy the saved title of the work you must come back to.",
            "id": "Use a clear name, such as west row or first batch.", "quantity": "E.g. 8 items. Enter the number only.",
            "done_quantity": "Enter the total finished so far, not just this update.", "checks": "E.g. permission received; items are ripe. One check per line.",
            "ready": "Enter yes only if the checks for this named part are complete.", "window": "E.g. 2026-10-05 09:00 / 2026-10-05 11:00.",
            "delay": "E.g. 30. The follow-up time is counted from the real start.", "minutes": "E.g. 25. Enter the number only.",
            "remove": "Copy the saved title of the earlier step that is no longer needed.", "add": "Leave blank to remove the old step without replacing it.",
            "kind": "Use one of the choices named above.", "mode": "Tentative means the time is still a proposal.",
            "ids": "Use the ready names shown on the card, separated by commas.", "tasks": "Copy each saved task title, separated by commas."}.get(key, "Use your own words.")
    return examples.replace("2026-10-05", day.isoformat()).replace("2026-10-04", (day - timedelta(days=1)).isoformat())


def choices(line, detail, state, ask):
    """Label, action, options; every mutation is narrowed again by the work door."""
    kind = line.get("kind", "")
    offered = set(line.get("offered", ()))
    if any(line.get("suggested_" + key) for key in ("name", "title", "label")):
        approval = "Use name and label" if line.get("suggested_label") and (line.get("suggested_name") or line.get("suggested_title")) else "Use this name" if line.get("suggested_name") or line.get("suggested_title") else "Use this label"
        answers = [(approval, "save", {}), ("Change name…", "rename", {"modal": True})]
        if "park" in offered:
            answers.append(("Put aside…", "when", {"mode": "park"}))
        # Dropping the item is an authenticated owner action, not approval of
        # its proposed name. Preserve question authority when it offers Drop.
        answers.append((drop_label(line, detail), "drop", {} if offered & {"end", "drop"} else {"whole_item": True}))
        return "Use this name and label?" if approval == "Use name and label" else "Use this name?" if approval == "Use this name" else "Use this label?", answers
    if kind == "outcome":
        return "How did it go?", [("Finished", "finished", {}), ("Made progress", "progress", {"modal": True}), ("Didn't do it", "not_done", {}), ("Something changed…", "update", {})]
    if kind == "did_it_happen":
        return "Did it happen?", [("Done", "done", {}), ("Rebook", "when", {"mode": "appointment"}), (drop_label(line, detail), "drop", {})]
    if kind == "carried_over" and not state.get("not_yet"):
        return "Did you do it?", [("Done", "done", {}), ("Not yet", "not_yet", {})]
    if kind == "missed":
        return "This deadline has passed. What should happen now?", [("Set a new deadline…", "when", {"mode": "deadline"}),
            ("Change", "change", {"modal": True}), ("Put aside…", "when", {"mode": "park"}), (drop_label(line, detail), "drop", {})]
    if kind == "needs_length":
        return "How much time does this need?", [(label, "length", {"value": value}) for label, value in (("15 min", 15), ("30 min", 30), ("1 hour", 60), ("2 hours", 120))]
    if kind == "two_minutes":
        return "Do it now?", [("Done now", "done", {}), ("Later", "when", {"mode": "planned"})]
    if kind == "to_chase":
        return "Time to follow up?", ([("Draft a follow-up", "ask_send", {"value": "nudge"})] if ask else []) + [("Skip for now", "skip", {})]
    if kind == "new_nothing_needed":
        return "Is this a note rather than something to do?", [("Keep as a note…", "classify", {"modal": True}), ("This needs action", "when", {"mode": "planned"}), (drop_label(line, detail), "drop", {})]
    if kind in {"carried_over", "new_schedule"} or detail.get("ref") in state.get("added", ()):
        return "When will you do it?", [("Today", "date", {"value": state["day"], "mode": "planned"}),
            ("Another day…", "when", {"mode": "planned"}), ("Someday", "park", {"value": "Someday"}),
            (drop_label(line, detail), "drop", {}), ("Change name…", "rename", {"modal": True})]
    if "parts" in offered:
        answers = [("Type the steps…", "parts", {"modal": True})]
        if ask:
            answers.append(("Ask Diablo", "ask", {}))
        return "What are the steps?", answers
    if "next_action" in offered:
        answers = [("Type the next step…", "next", {"modal": True})]
        if ask:
            answers.append(("Ask Diablo", "ask", {}))
        answers.append(("Put aside…", "when", {"mode": "park"}))
        if kind == "nothing_next":
            answers.insert(0, ("Done", "done", {}))
        if kind == "stalled":
            answers.append((drop_label(line, detail), "drop", {}))
        return "What's the next step?" if kind != "stalled" else "What's in the way? What's the next physical thing?", answers
    answers = []
    if "accept" in offered:
        answers.append(("Yes, save", "save", {}))
    if "title" in offered:
        answers.append(("Change name…", "rename", {"modal": True}))
    if "park" in offered:
        answers.append(("Put aside…", "when", {"mode": "park"}))
    return line.get("text", "What does this need?").split(" — ", 1)[-1], answers


def _render(state, view, issue, *, emojis=None, dashboard_url=None, ask=False, draft=None, pending=None, day_count=5):
    day, screen = date.fromisoformat(state["day"]), state["screen"]
    icons = emojis or {}
    def icon(name):
        return icons.get("exo_" + name, "")
    def tags(labels):
        return "  ›  ".join((icon("tag_" + COLOURS[int(hashlib.sha256(label.encode()).hexdigest(), 16) % len(COLOURS)]) + " " + plain(label, 80)).strip() for label in labels)
    def button(label, action, **options):
        disabled = options.pop("disabled", False)
        style = 4 if action == "drop" else 3 if action in {"start", "save", "confirm", "draft_save"} else 2
        return {"type": 2, "label": label[:80], "style": style, "custom_id": issue(label, action, options), "disabled": disabled}
    def nav(skip=False):
        previous = state.get('history', [{}])[-1] if state.get('history') else {}
        destination = {'day': 'choices', 'day_more': 'choices', 'open': 'overview', 'yesterday': 'review', 'inbox': 'review', 'attention': 'review', 'update': 'item', 'inspect': 'item', 'item_more': 'item', 'planning_menu': 'planning', 'selector': 'choices', 'planning_details': 'checks'}
        buttons = [button('← Back to ' + destination.get(previous.get('screen'), 'review'), 'back')]
        if skip:
            buttons.append(button("Skip for now", "skip"))
            buttons.append(button("Add things…", "add", modal=True))
            if screen not in {"update", "inspect", "planning_menu", "item_more"} and view.get("line", {}).get("kind") != "outcome":
                buttons.append(button("Something changed…", "update"))
            buttons.append(button("Today's choices", "go_day"))
        return row(*buttons)
    head = [text("-# Morning review · " + day_label(day) + " · " + state.get("timezone", "Asia/Singapore"))]
    if state.get("win") and screen != "settled" and not state.get("error") and not pending:
        head.append(text("Recorded · " + plain(state["win"], 240)))
    if state.get("error"):
        head.append(text(plain(state["error"], 400)))
    questions = {"open": "Today at a glance", "catch": "What happened since yesterday?", "new": "Inbox · what does this need?",
                 "yesterday": "Yesterday: how did it go?", "inbox": "Inbox · what does this need?", "attention": "Needs attention",
                 "day": "Choose today's work", "lengths": "How long will each take?"}
    stage = state.get("stage", "new") if screen in {"when", "item_length", "ask", "update", "item_more", "planning_menu"} else screen
    if stage in questions:
        head.append(text("### " + questions[stage]))
    if state.get("date_changed"):
        head.append(text("It is a new local date. Your answers and choices are kept. Recheck the carried choices for today before saving."))
    if state.get("submitted_draft") and (pending or screen not in {"day_summary", "settled"}):
        kept_value = state["submitted_draft"].get("value")
        head.append(text("Your submitted answer is saved. Check the item before using it: “" + plain(kept_value, 400) + "”"
                         if kept_value else "Your submitted form fields are kept. Reopen the form to check them."))
        if state["submitted_draft"].get("spec", {}).get("action") == "add" and screen != "kept_addition":
            head.append(row(button("Review earlier submission", "review_addition")))
        elif state["submitted_draft"].get("spec", {}).get("action") != "add" and state["submitted_draft"].get("ref"):
            target = view.get("details", {}).get(state["submitted_draft"]["ref"])
            if not target or target.get("state") in {"done", "dropped", "no_longer_needed"}:
                head.append(text("The item for this kept answer is closed or unavailable. The answer stays kept; it cannot be applied to a different item."))
    if state.get("last_drop") and not pending:
        dropped = state["last_drop"]
        head.append(text("Recently dropped · " + plain(dropped["title"], 120) + " · " + dropped["scope"] + ". Restore checks the exact affected records; later changes can prevent restoration."))
        head.append(row(button("Restore recent drop", "undo_drop", change=dropped["change"], ref=dropped["ref"])))
    if screen == "expired":
        return payload(head + [text("Replaced by today's review")])
    if pending:
        return payload(head + [container([text("The earlier save has not been confirmed yet."),
            row({"type": 2, "label": "Check save again", "style": 3, "custom_id": pending["token"]})]), nav()])
    if screen in {'return_change', 'return_change_form', 'return_preview'}:
        change = state['return_change']
        if screen == 'return_change':
            body = [text('## Change the required return'), text(full_text(change['source_title'])),
                    text('The existing return remains required until the exact change is approved.'),
                    row(button('Transfer to another task', 'return_transfer'), button('No longer required', 'return_discharge'))]
        elif screen == 'return_change_form':
            body = [text('## Explain this return change'), text('Naming someone does not notify them. Confirm responsibility only after agreement.'),
                    row(button('Explain this change…', 'return_reason', modal=True, kind=change['kind'], selected_fields={'source':change['source'], 'from_ref':change['from_ref'], 'to_ref':change.get('to_ref')}))]
        else:
            disposition = change['disposition']
            preview = ('Source: ' + change['source_title'] + '\nOriginal return: ' + change['original_title'] +
                '\nCurrent required return: ' + change['active_title'] + '\nOriginal due time: ' + datetime.fromisoformat(change['at']).astimezone(ZoneInfo(state.get('timezone', 'Asia/Singapore'))).strftime('%d %b %Y %H:%M %Z') +
                ('\nReplacement: ' + change['replacement_title'] + '\nAgreed responsible person: ' + disposition['assignee'] +
                 '\nResponsibility explicitly agreed. No notification or delivery is implied.' if disposition['kind'] == 'transfer' else
                 '\nDisposition: this return is no longer required.') + '\nOwner reason: ' + disposition['reason'] +
                 '\nNeither task is marked finished. Original actual-start history is kept.')
            pages = text_pages(preview, 900)
            page = min(state.get('return_preview_page', 0), len(pages)-1)
            body = [text('## Check the complete return change'), text(f'Page {page+1} of {len(pages)}'), text(pages[page])]
            if page:
                body.append(row(button('Previous preview', 'return_preview_page', value=page-1)))
            if page+1 < len(pages):
                body.append(row(button('More preview', 'return_preview_page', value=page+1)))
            else:
                body.append(row(button('Approve return change', 'followup_disposition', ref=change['source'],
                    disposition=disposition, disposition_expected=change['expected'])))
        return payload(head + [container(body), nav()])
    if screen == 'selector':
        picker = state['selector']
        field = view['selector_field']
        items = view['selector_items']
        page = view['selector_page']
        labels = {'source': 'Which started operation?', 'to_ref': 'Which replacement return?', 'item': 'Which work?', 'value': 'Which project?', 'remove': 'Which earlier step is no longer required?', 'add': 'Which new earlier step?', 'followup': 'Which required return?', 'tasks': 'Which tasks share this block?', 'ids': 'Which ready parts?', 'id': 'Which part are you updating?'}
        body = [text('## ' + labels[field]), text('Choose the saved item below. Project and state distinguish matching names.')]
        if page:
            allowed = {str(number): item for number, item in enumerate(page)}
            selected = picker['selected'].get(field, [])
            if isinstance(selected, str):
                selected = [selected]
            body.append(row({'type': 3, 'custom_id': issue('Choose saved work', 'selector_choose', {'field': field, 'allowed': allowed}),
                'placeholder': 'Choose from this page', 'min_values': 0 if field in {'tasks', 'ids'} else 1, 'max_values': len(page) if field in {'tasks', 'ids'} else 1,
                'options': [{'label': plain(item['label'], 95), 'description': plain(item['description'], 95), 'value': value, 'default': item['ref'] in selected} for value,item in allowed.items()]}))
            if field in {'tasks', 'ids'}:
                body += [text(str(len(selected)) + ' selected across all pages.'), row(button('Use these choices', 'selector_continue'))]
        else:
            body.append(text('No applicable saved work here. Use Back to return or add the missing work first.'))
        navigation = []
        if picker['offset']:
            navigation.append(button('Previous choices', 'selector_page', offset=picker['offset'] - 25))
        if picker['offset'] + 25 < len(items):
            navigation.append(button('More choices', 'selector_page', offset=picker['offset'] + 25))
        if navigation:
            body.append(row(*navigation))
        if field == 'add':
            body.append(row(button('No replacement', 'selector_none', field='add')))
        return payload(head + [container(body), nav()])
    if screen == 'form_ready':
        picker = state['selector']
        lines = []
        for field,value in picker['selected'].items():
            values = value if isinstance(value, list) else [value]
            names = [view.get('details', {}).get(ref, {}).get('title', ref or 'No replacement') for ref in values]
            lines.append(field.title() + ': ' + ', '.join(names))
        body = [text('## Check the chosen context'), text('\n'.join(plain(line, 250) for line in lines)),
                text('The next form is bound to these saved choices. A changed record requires a fresh choice.'),
                row(button('Continue with these choices…', 'bound_form', modal=True))]
        return payload(head + [container(body), nav()])
    if screen == 'arrival_saved':
        context = state.get('arrival_context', {})
        body = [text('## Arrival recorded · ' + plain(state.get('arrival_title'), 150)),
                text('The wait is cleared. This work remains open; it has not been marked done.')]
        if context.get('who') or context.get('what'):
            body.append(text('Previously waiting on ' + plain(context.get('who'), 120) + ' · ' + plain(context.get('what'), 180)))
        if context.get('expected'):
            body.append(text('Previous check date: ' + plain(context['expected'], 80)))
        body.append(row(button('Continue choosing', 'go_day')))
        return payload(head + [container(body), nav()])
    if screen in {'day_more', 'open_more'}:
        details = view.get('details', {})
        body = [text('## More day options'), row(button("Today's choices", 'go_day'))]
        groups = [[button('Add things', 'add', modal=True), button('Local time zone…', 'timezone', modal=True)]]
        if any(card.get('state') == 'done' for card in details.values()):
            groups.append([button('Correct completed work…', 'correction', modal=True)])
        if any(card.get('state') not in {'done', 'dropped', 'no_longer_needed'} for card in details.values()):
            groups.append([button('Update an item…', 'inspect', modal=True), button('Set a work session…', 'timebox', modal=True)])
        if any(has_ready(card['ref'], view) for card in details.values()):
            groups.append([button('Choose ready parts or batches…', 'portion_select', modal=True)])
        if any(record.data.get('after') for record in view.get('index', {}).values()):
            groups.append([button('Change an earlier step…', 'requirement', modal=True)])
        groups.append([button('Keep time free…', 'hold', modal=True)])
        if any(card.get('status') == 'waiting' and card.get('arrival_available') for card in details.values()):
            groups.append([button('Update something waiting…', 'waiting_updates')])
        for group in groups:
            body.append(row(*group))
        return payload(head + [container(body), nav()])
    if state.get('interrupted_review') and screen in {'open', 'day', 'day_more', 'settled', 'anything'}:
        head.append(row(button('Continue review', 'resume_review')))
    data = view["day"]
    if screen in {"open", "settled"}:
        timeline = data.get("timeline", ())
        def times(slot):
            stamp = int(datetime.fromisoformat(slot["start"]).timestamp())
            return ("All day" if slot.get("all_day") else f"<t:{stamp}:t> · <t:{stamp}:R>") + " · " + plain(slot["title"], 120)
        events = [times(slot) for slot in timeline if slot["type"] == "event"]
        events += [plain(card["title"], 100) + " · " + (appointment_label(card, state.get("timezone", "Asia/Singapore")) or " · ".join(plain(f, 80) for f in card.get("facts", ())[:2])) for card in data.get("today", ())
                   if card["ref"] not in {slot.get("ref") for slot in timeline}]
        schedule_pages = text_pages(schedule_text(state, data), 750)
        schedule_page = min(max(state.get('schedule_page', 0), 0), len(schedule_pages)-1)
        schedule_controls = [button('Check schedule', 'schedule_only')]
        if schedule_page:
            schedule_controls.append(button('Previous schedule page', 'schedule_page', value=schedule_page-1))
        if schedule_page + 1 < len(schedule_pages):
            schedule_controls.append(button('Next schedule page', 'schedule_page', value=schedule_page+1))
        if screen == "open":
            def bounded(lines, count):
                return "\n".join(lines[:count]) + (f"\n{len(lines) - count} more · open My day" if len(lines) > count else "")
            boxes = [container([text("### Fixed times and commitments"), text(bounded(events, 5) or "Nothing at a set time.")], GOLD),
                container([text("### Latest schedule"), text(schedule_pages[schedule_page]),
                           row(*schedule_controls)])]
            for key, title, colour in (("due_today", "Due today", RED), ("due_tomorrow", "Due tomorrow", WHITE)):
                if data.get(key):
                    boxes.append(container([text("### " + title), text(bounded([plain(card["title"], 80) for card in data[key]], 4))], colour))
            notices = [notice for notice in data.get("notices", ()) if notice != "Couldn't see the schedule"]
            if notices:
                boxes.append(text("\n".join(plain(notice, 120) for notice in notices[:3])))
            return payload(head + boxes + [row(button("Start review", "start"), button("Today's choices", "go_day"), button("More options…", "open_more"))])
        saved = state.get('saved_choices')
        saved_text = saved.get("text", "") if saved else "The earlier saved choice list is unavailable in this card. Open today's choices to review current facts."
        pages = text_pages(saved_text, 750)
        page = min(max(state.get('saved_page', 0), 0), len(pages)-1)
        body = [text("## Choices saved for today"),
                text('Confirmed choices at save time. Current facts and schedule may have changed.'),
                text(f"Saved choices page {page+1} of {len(pages)}"), text(pages[page])]
        paging = []
        if page:
            paging.append(button('Previous saved page', 'saved_page', value=page-1))
        if page + 1 < len(pages):
            paging.append(button('Next saved page', 'saved_page', value=page+1))
        if paging:
            body.append(row(*paging))
        body += [text(schedule_pages[schedule_page]), row(*schedule_controls)]
        controls = row(button("Change plan", "go_day"), button("More options…", "day_more"))
        body.append(controls)
        if dashboard_url:
            url = dashboard_url.rstrip("/")
            controls["components"].append({"type": 2, "style": 5, "label": "Open my day", "url": url if url.endswith("/app") else url + "/app"})
        if day.weekday() == 4:
            body.append(row(button("Weekly review", "weekly")))
        return payload(head + [container(body, GOLD)])
    if screen == "waiting_updates":
        cards = view.get("waiting_items", ())
        start = state.get("page", 0) * 5
        body = [text("## Has anything you were waiting for arrived?"),
                text("These stay out of today’s choices until the wait is cleared. This list opens only when you ask.")]
        for card in cards[start:start + 5]:
            from .selectors import wait_context
            context = card.get('wait_context') or wait_context(card, view.get('index', {}))
            description = '**' + plain(card['title'], 160) + '**'
            if context.get('who') or context.get('what'):
                description += '\nWaiting on ' + plain(context.get('who'), 100) + ' · ' + plain(context.get('what'), 180)
            if context.get('expected'):
                description += '\nCheck date: ' + plain(context['expected'], 80)
            body.append({"type": 9, "components": [text(description)],
                         "accessory": button("Update…", "waiting_item", ref=card["ref"])})
        if not cards:
            body.append(text("Nothing is waiting for an outside reply or delivery."))
        if start + 5 < len(cards):
            body.append(row(button("Show more", "more")))
        return payload(head + [container(body), nav()])
    if screen == "answered":
        card = view.get("item") or {}
        pages = text_pages(card.get("notes") or "No saved note for this item.", 750)
        page = min(max(state.get("page", 0), 0), len(pages)-1)
        body = [text("## " + plain(card.get("title") or state.get("review_title") or "Reviewed item", 160)),
                text("Previous item. Going back does not undo saved answers."),
                text("Current saved notes (read only)"), text(f"Page {page+1} of {len(pages)}"), text(pages[page])]
        paging = []
        if page:
            paging.append(button("Previous saved notes page", "answer_page", value=page-1))
        if page+1 < len(pages):
            paging.append(button("Next saved notes page", "answer_page", value=page+1))
        if paging:
            body.append(row(*paging))
        body.append(row(button("Continue review", "resume_review")))
        return payload(head + [container(body), nav()])
    if screen == "kept_addition":
        kept = state.get("submitted_draft", {})
        return payload(head + [container([text("## Review your submitted addition"), text(plain(kept.get("value"), 1000)), text("Save these words as captured work. Any matching existing items will be checked before saving."), row(button("Save these items", "reuse_addition"))]), nav()])
    if screen == "duplicate":
        capture = state.get("capture_parts", [{}])[state.get("duplicate_part", 0)]
        refs = state.get("duplicate_matches", [{}])[0].get("refs", ())
        body = [text("## This looks like something already saved"), text("Your captured step: “" + plain(capture.get("title"), 400) + "”")]
        if capture.get("then") and state.get("duplicate_part", 0) > 0:
            predecessor = state["capture_parts"][state["duplicate_part"] - 1]["title"]
            body.append(text("If you update the existing item, it will wait for “" + plain(predecessor, 200) + "”. Its other earlier requirements stay in place."))
        if state.get("duplicate_ref"):
            card = view["details"].get(state["duplicate_ref"], {})
            body += [text(plain(card.get("title"), 160)), row(button("Update existing", "duplicate_attach", ref=card.get("ref")), button("Keep separate", "duplicate_add"))]
        else:
            body += [text("Several saved items have this name. Choose by project and state, or keep this capture separate."),
                     row(button("Choose existing\u2026", "duplicate_choose", modal=True), button("Keep separate", "duplicate_add"))]
        return payload(head + [container(body), nav()])
    if screen == "anything":
        return payload(head + [container([text("## Anything else to add?"), text("Add anything missing, then choose what to work on today."), gap(2),
            row(button("Add things", "add", modal=True), button("Choose today's work", "go_day"))]), nav()])
    if screen == "planning_details":
        planning = view.get("planning", {})
        lines = [*planning.get("issues", ()), *planning.get("notices", ())]
        for followup in planning.get("conditional_followups", ()):
            title = view.get("details", {}).get(followup["item_ref"], {}).get("title", "Required follow-up")
            timing = "time counted from when the work actually starts"
            if not followup.get("conditional"):
                timing = datetime.fromisoformat(followup["at"]).astimezone(ZoneInfo(state.get("timezone", "Asia/Singapore"))).strftime("%d %b %H:%M %z") if followup.get("at") else "time needs confirmation"
            lines.append("Required: " + title + " · " + timing)
        start = state["page"] * 5
        body = [text("## Time and follow-ups to check"), text("\n".join(plain(line, 350) for line in lines[start:start + 5]) or "No additional checks.")]
        from .selectors import catalog
        if catalog('return_change', 'source', view, {}, None):
            body.append(row(button('Change required return', 'return_change')))
        if start + 5 < len(lines):
            body.append(row(button("More checks", "more")))
        body.append(row(button("Continue choosing", "go_day")))
        return payload(head + [container(body), nav()])
    if screen == "weekly":
        sections = view.get("weekly", ())
        section = sections[min(state["page"], len(sections) - 1)] if sections else {"title": "Weekly review", "lines": ["Nothing to review this week."]}
        body = [text("## " + section["title"]), text("\n".join(plain(line, 220) for line in section["lines"]))]
        if state["page"] + 1 < len(sections):
            body.append(row(button("Next section", "weekly_next")))
        return payload(head + [container(body), nav()])
    if screen == "day_summary":
        approval = state.get("day_approval", {})
        pages = text_pages(approval.get("text", "No choices to review."), 750)
        page = min(max(state.get("page", 0), 0), len(pages)-1)
        body = [text("## Review today's complete choices"), text(f"Page {page+1} of {len(pages)}"), text(pages[page])]
        paging = []
        if page:
            paging.append(button("Previous summary page", "summary_page", value=page-1))
        if page + 1 < len(pages):
            paging.append(button("Next summary page", "summary_page", value=page+1))
        if paging:
            body.append(row(*paging))
        body.append(row(*([button("Save today's choices", "confirm")] if page + 1 == len(pages) else []), button("Change choices", "go_day")))
        planned = [card for card in view.get("details", {}).values() if card.get("state") == "open" and card.get("dates", {}).get("planned", {}).get("day") == state["day"]]
        if planned:
            body.append(text("Removing a planned date keeps that task available. It is separate from removing focus."))
            body.append(row(button("Remove a planned date…", "remove_plan", modal=True)))
        return payload(head + [container(body), nav()])
    if screen == "day":
        planning = view.get("planning", {})
        for notice in [*planning.get("issues", ()), *planning.get("notices", ())][:3]:
            head.append(text(plain(notice, 250)))
        if planning.get("issues") or planning.get("notices") or planning.get("conditional_followups"):
            head.append(row(button("Check time and follow-ups", "planning_details")))
        head.append(text("-# Most important first · selected work is marked Today"))
        candidates = view.get("candidates", ())
        start = state.get("day_offset", state["page"] * 5)
        body = []
        ready = view.get("planning", {}).get("ready_portions", {})
        offers = [plain(view["details"].get(ref, {}).get("title", "Work"), 60) + " · " + ", ".join(plain(p["id"], 35) + " × " + str(p.get("remaining_quantity", p["quantity"])) for p in portions)
                  for ref, portions in ready.items() if portions]
        if offers:
            body.append(text("**Ready parts or batches**\n" + "\n".join(offers[:3]) + "\nChoose the named ready portions; later portions stay separate."))
        for card in candidates[start:start + day_count]:
            sub = tags([card["parent"]["title"]] if card.get("parent") else card.get("labels", ()))
            sub += "\n" + (card.get("schedule_label") or (str(card["length_minutes"]) + " min" if card["length_minutes"] else "length not set yet"))
            if card.get("stake"):
                sub += "\n" + icon("stake") + " Big one: " + plain(card["stake"], 100)
            body.append({"type": 9, "components": [text("**" + plain(card["title"], 100) + "**\n" + sub)],
                         "accessory": button("● Today" if card["picked"] else "○ Add", "toggle", ref=card["ref"])})
            body.append(gap())
        if start + day_count < len(candidates):
            body.append(row(button("Show more", "more", next_offset=start + day_count)))
        elif start:
            body.append(row(button("Show first", "first")))
        if view.get("waiting"):
            body.append(text(plain(view["waiting"], 200)))
        body.append(row(button('Set available hours…', 'capacity', modal=True), button('More options…', 'day_more')))
        picked_count = sum(card["picked"] for card in candidates)
        body.append(row(button("Review choices · " + str(picked_count) + (" focus task" if picked_count == 1 else " focus tasks"), "review_choices")))
        if not candidates:
            body.insert(0, text("Nothing to pick today."))
        navigation = nav()
        while len(list(walk(head + [container(body), navigation]))) > 40:
            position = next((i for i, part in enumerate(body) if part["type"] == 14), None)
            if position is None:
                break
            body.pop(position)
        return payload(head + [container(body), navigation])
    if screen == "step_context":
        pages = text_pages(step_context(view, state["steps_ref"]), 750)
        page = min(max(state.get("steps_page", 0), 0), len(pages) - 1)
        controls = []
        if page:
            controls.append(button("Previous steps page", "existing_steps", ref=state["steps_ref"], value=page - 1))
        if page + 1 < len(pages):
            controls.append(button("Next steps page", "existing_steps", ref=state["steps_ref"], value=page + 1))
        body = [text("## Existing steps · page " + str(page + 1) + " of " + str(len(pages))), text(pages[page]),
                text("Adding steps preserves this work and its requirements. New ordered steps wait for the preceding new step; changing an existing prerequisite requires a separate explicit change.")]
        if controls:
            body.append(row(*controls))
        body.append(row(button("Add steps…", "parts", modal=True, ref=state["steps_ref"])))
        return payload(head + [container(body), nav()])
    card = view.get("item") or {}
    line = view.get("line") or {}
    if screen in {"when", "item_length", "lengths", "ask", "catch", "new", "yesterday", "inbox", "attention", "update", "item_more", "planning_menu", "inspect"}:
        queue = view.get("queue", ())
        if queue and screen in {"catch", "new", "yesterday", "inbox", "attention", "inspect"}:
            snapshot = state.get('queue_snapshots', {}).get(stage, [value['reference'] for value in queue])
            added = state.get('queue_additions', {}).get(stage, [])
            current_ref = state['item']
            if current_ref in snapshot:
                progress = str(snapshot.index(current_ref) + 1) + ' of ' + str(len(snapshot))
            else:
                progress = 'Added item ' + str(added.index(current_ref) + 1 if current_ref in added else 1) + ' · ' + str(len(snapshot)) + ' original items'
            deferred = sum(ref in state.get('skipped', []) for ref in snapshot + added)
            progress += ' · ' + str(deferred) + ' deferred' if deferred else ''
            progress += ' · ' + str(len(added)) + ' added during review' if added else ''
            head.append(text('-# ' + progress))
        labels = card.get("crumbs", ([card["parent"]["title"]] if card.get("parent") else [])) + card.get("labels", [])
        body = ([text(tags(labels)), gap()] if labels else []) + [text("## " + plain(line.get("suggested_title") or line.get("suggested_name") or card.get("title") or line.get("text"), 160))]
        facts = list(card.get("facts", ()))
        local_appointment = appointment_label(card, state.get("timezone", "Asia/Singapore"))
        if local_appointment:
            facts.insert(0, local_appointment)
        if facts:
            body.append(text("\n".join((icon("date") + " " + plain(fact, 150)).strip() for fact in facts[:4])))
        if card.get("progress", {}).get("total"):
            body.append(text(icon("steps") + " " + plain(card["progress"]["text"], 160)))
        if card.get("next_after"):
            body.append(text(icon("after") + " Next after this: **" + plain(card["next_after"], 120) + "**"))
        if card.get("stake"):
            body.append(text(icon("stake") + " Big one: " + plain(card["stake"], 140)))
        if line.get("suggested_title") or line.get("suggested_name") or line.get("suggested_label"):
            body += [text(icon("note") + " Your note said “" + plain(card.get("original_words") or card.get("title"), 160) + "”"),
                     text(icon("diablo") + " Suggested wording · check it before saving")]
            if line.get("suggested_label"):
                body.append(text("Suggested label: **" + plain(line["suggested_label"], 120) + "**"))
        elif card.get("ref") in state.get("added", ()):
            body.append(text("Just added"))
            if state.get("tidying"):
                body.append(text(icon("working") + " Diablo is tidying…"))
        body.append(gap(2))
        if any(r.data.get("parent") == card.get("ref") for r in view.get("index", {}).values()):
            body.append(row(button("View existing steps and blockers", "existing_steps", ref=card["ref"], value=0)))
        if card.get("planning", {}).get("portions"):
            body.append(text("**Named parts or batches**\n" + "\n".join(plain(p["id"], 60) + " · " + str(p["quantity"]) + " · " + ("checks confirmed" if p.get("ready") and p.get("checks") else "not ready") for p in card["planning"]["portions"][:6])))
        if line.get("commitment_day"):
            body.append(text("-# You planned this for " + day_label(date.fromisoformat(line["commitment_day"])) + "."))
        if screen in {'update', 'inspect'}:
            primary = []
            if card.get('status') == 'waiting' and card.get('arrival_available'):
                primary.append(button('What I needed is here…', 'arrival', modal=True))
            else:
                primary.append(button('Waiting for something…', 'wait', modal=True))
            primary.append(button('Reschedule appointment…' if line.get('scope') == 'appointment' else 'Move to another day…', 'when', mode='appointment' if line.get('scope') == 'appointment' else 'planned'))
            body += [text('### What changed?'), row(*primary), row(button(drop_label(line, card), 'drop'), button('More options…', 'item_more'))]
            if screen == 'inspect':
                body.append(row(button('Finished', 'finished'), button('Made progress', 'progress', modal=True)))
        elif screen == 'item_more':
            body += [text('### More options for this item'),
                     row(button('Put aside…', 'when', mode='park'), button('Keep as a note…', 'classify', modal=True)),
                     row(button('Add to a project…', 'under', modal=True), button('Someone else will do it…', 'delegate', modal=True)),
                     row(button('More planning details…', 'planning_menu'), button('Describe another change…', 'note', modal=True))]
            if line.get('scope') in {'appointment', 'occurrence'}:
                body.append(row(button('Canceled', 'canceled'), button('No show', 'no_show'), button('Move appointment…', 'appointment', modal=True)))
            body.append(row(button('Record another date…', 'dated_outcome', modal=True)))
            draft_words = state.get('submitted_draft')
            if draft_words and draft_words.get('ref') == card.get('ref'):
                body.append(row(button('Use earlier answer for this item', 'apply_draft')))
        elif screen == "planning_menu" and state.get("page", 0) == 0:
            body += [text("### Plan the details"),
                     text("Does this need to happen within certain hours? Otherwise, go Back to continue."),
                     row(button("Set when this can happen…", "planning_window", modal=True)),
                     row(button("More options…", "planning_menu", value="more"))]
        elif screen == 'planning_menu':
            body += [text('### More planning options'), text('Use these only if this task needs extra arrangements.'),
                     row(button('Book shared equipment…', 'planning_resource', modal=True), button('Add a part or batch…', 'planning_portion', modal=True))]
            operation = card.get('planning', {}).get('operation') or {}
            if not operation:
                body.append(row(button('Agree the start and follow-up…', 'planning_operation', modal=True)))
            elif operation.get('approved') and not operation.get('actual_start'):
                body.append(row(button('Record when it started…', 'planning_start', modal=True), button('Cancel before starting', 'planning_cancel')))
            elif operation.get('actual_start'):
                body.append(text('Actual start is recorded. Required returns remain visible under Time and follow-ups.'))
            if card.get('planning', {}).get('portions'):
                buttons = [button('Update a part or batch…', 'portion_result', modal=True)]
                if has_ready(card.get('ref'), view):
                    buttons.append(button('Choose ready parts or batches…', 'portion_select', modal=True))
                body.append(row(*buttons))
            body.append(row(button('Set when this can happen…', 'planning_window', modal=True), button('Share a trip or setup…', 'planning_shared', modal=True)))
        elif screen == "when":
            monday = day + timedelta(days=(7 - day.weekday()) or 7)
            buttons = [button("Tomorrow", "date" if state["mode"] != "park" else "park", value=(day + timedelta(days=1)).isoformat(), mode=state["mode"]),
                button(day_label(monday), "date" if state["mode"] != "park" else "park", value=monday.isoformat(), mode=state["mode"]),
                button("In a week · " + day_label(day + timedelta(days=7)), "date" if state["mode"] != "park" else "park", value=(day + timedelta(days=7)).isoformat(), mode=state["mode"])]
            if state["mode"] == "park":
                buttons.append(button("Someday", "park", value="Someday"))
            body += [text("### When?"), row(*buttons), row(button("Pick a date…", "park" if state["mode"] == "park" else "date", value="picker", modal=True, mode=state["mode"]))]
        elif screen in {"item_length", "lengths"}:
            body += [text("### How much time does this need?"), row(*(button(label, "length", value=value) for label, value in (("15 min", 15), ("30 min", 30), ("1 hour", 60), ("2 hours", 120)))),
                     text("Estimate the whole task. A work session is a separate bounded attempt."),
                     row(button("Other minutes…", "length_custom", modal=True), button("Not sure", "length_unknown"))]
        elif screen == "ask":
            presets = [button("Draft the next step", "ask_send", value="next")]
            if set(line.get("offered", ())) & {"parts", "add_parts"}:
                presets.insert(0, button("Research and break it down", "ask_send", value="research"))
            body += [text("### What should Diablo do?"), row(*presets), row(button("In my own words…", "ask_words", modal=True))]
        elif draft and draft.get("state") in {"waiting", "draft", "nudge"}:
            if draft["state"] == "waiting":
                body += [text(icon("working") + " **Diablo is on it.** His draft will show up here."), row(button("Working…", "refresh", disabled=True))]
            elif draft["state"] == "nudge":
                body += [text("### Diablo's suggestion"), text(plain(draft["text"], 1500)),
                         text("-# Reply in the thread below to change it")]
            else:
                steps = draft["steps"]
                pages = [page for i, step in enumerate(steps, 1) for page in
                         text_pages(f"Step {i} of {len(steps)}\n{step['title']}\n{step.get('detail', '')}", 750)]
                page = min(max(state.get("draft_page", 0), 0), len(pages) - 1)
                binding = dict(draft_operation_id=draft["operation_id"], draft_sha256=draft["draft_sha256"])
                paging = []
                if page:
                    paging.append(button("Previous draft page", "draft_page", value=page - 1, **binding))
                if page + 1 < len(pages):
                    paging.append(button("Next draft page", "draft_page", value=page + 1, **binding))
                body += [text("### Complete draft · " + str(len(steps)) + " steps · page " + str(page + 1) + " of " + str(len(pages))),
                         text(pages[page]), text("Approval saves all steps and details in this exact draft. Reply in the thread below to change it.")]
                if paging:
                    body.append(row(*paging))
                body.append(row(button("Save this step" if len(steps) == 1 else "Save these steps", "draft_save", **binding)))
        else:
            if draft and draft.get("state") not in {None, "none"}:
                body.append(text(plain(draft.get("text") or "Diablo's draft couldn't be used. Try again.", 250)))
            question, answers = choices(line, card, state, ask)
            body.append(text("### " + plain(question, 300)))
            for offset in range(0, len(answers), 4):
                body.append(row(*(button(label, action, **options) for label, action, options in answers[offset:offset + 4])))
        return payload(head + [container(body), nav(skip=screen in {"catch", "new", "yesterday", "inbox", "attention", "inspect"})])
    raise CardError("Couldn't open that screen. Return to today's review.")


def render(state, view, issue, *, emojis=None, dashboard_url=None, ask=False, draft=None, pending=None):
    """Reserve the complete header/footer budget before choosing page size.

    Offsets on More controls keep navigation exact when notices change the
    available space. All other oversized views have independent small exits.
    """
    for count in range(5, 0, -1):
        offered = {}
        def preview(label, action, options):
            token = "preview:" + str(len(offered))
            offered[token] = (label, action, deepcopy(options))
            return token
        try:
            result = _render(state, view, preview, emojis=emojis, dashboard_url=dashboard_url,
                             ask=ask, draft=draft, pending=pending, day_count=count)
        except CardRenderError:
            if state["screen"] != "day":
                break
        else:
            # Only the controls in the accepted payload get durable tokens.
            bound = {}
            for node in walk(result["components"]):
                token = node.get("custom_id")
                if token in offered:
                    if token not in bound:
                        bound[token] = issue(*offered[token])
                    node["custom_id"] = bound[token]
            return result
    controls = []
    if pending:
        controls.append({"type": 2, "style": 3, "label": "Check save again", "custom_id": pending["token"]})
    else:
        for label, action in (("Today's choices", "go_day"), ("Back to review", "start")):
            controls.append({"type": 2, "style": 2, "label": label, "custom_id": issue(label, action, {})})
    return payload([text("This view could not fit in Discord. Your submitted answers and choices are kept."
                         + (" The earlier save is unconfirmed; check that exact save again." if pending else " Open a smaller review view to continue.")),
                    row(*controls)])
