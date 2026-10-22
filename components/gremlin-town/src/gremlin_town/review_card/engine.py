"""One owner, fresh records on every redraw, durable exact-tap retries."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime
import hashlib
import json
from zoneinfo import ZoneInfo, available_timezones

from brain_core import work
from gremlin_town.mcp.brain_runtime import BrainRuntimeError, WorkRequest
from .capture import capture_parts
from .actions import make_request, request_data, restore_request, words
from .backend import ReviewCardBackend
from .diablo import DiabloRefused
from .models import CardError, CardResult, expires, location, review_day, screen_key
from .render import modal, render
from .selectors import PICKER_ACTIONS, fields_for, page_for, proof, wait_context
from .latency import phase, mark_action


def _form_key(spec):
    key = [spec.get("action"), spec.get("ref"), spec.get("value")]
    if spec.get("selected_fields"):
        key += [spec["selected_fields"], spec.get("kind")]
    return json.dumps(key, separators=(",", ":"), sort_keys=True)


class ReviewCardEngine:
    def __init__(self, runtime, settings, store, clock, diablo=None, *, backend=None, emojis=None, auto_tidy=None, tidy_revision=None):
        self.runtime, self.settings, self.store, self.clock = runtime, settings, store, clock
        self.backend = backend or ReviewCardBackend(runtime, settings)
        self.diablo, self.emojis, self.auto_tidy = diablo, emojis or {}, auto_tidy
        self.tidy_revision = tidy_revision
        self._result_revision = None
        from brain_core.timewords import SINGAPORE
        zone = store.latest_timezone() if hasattr(store, "latest_timezone") else "Asia/Singapore"
        self.planning_zone = SINGAPORE if zone == "Asia/Singapore" else ZoneInfo(zone)
        self.lock = asyncio.Lock()

    def _day(self):
        from brain_core.timewords import SINGAPORE
        return review_day(self.clock(), self.planning_zone or SINGAPORE)

    def can_continue(self, token, *, submitted=False):
        return self.store.can_continue(token, submitted=submitted)

    def _owner(self, value):
        if not self.settings.enabled or not self.settings.card:
            raise CardError("The morning card is switched off.")
        if value != self.settings.owner_user_id:
            raise CardError("This review belongs to its configured owner.")

    def bind_message(self, day, message_id):
        return self.store.bind_message(day, message_id)

    def active_message_id(self):
        return self.store.active_message_id()

    async def open(self, day=None):
        async with self.lock:
            if not self.settings.enabled or not self.settings.card:
                raise CardError("The morning card is switched off.")
            day = date.fromisoformat(day) if isinstance(day, str) else day or self._day()
            state = self.store.session(day)
            state["day"] = day.isoformat()
            return await self._draw(state)

    async def current(self):
        return await self.open()

    async def _local_revision(self):
        day = str(self._day())
        ask = await self.diablo.revision(day) if self.diablo and self.settings.ask_diablo else None
        tidy = self.tidy_revision(self.clock()) if self.tidy_revision and self.settings.auto_tidy else None
        return (day, ask, tidy)

    async def refresh_pending(self):
        """Local accepted-result fingerprints first; no periodic Brain/calendar reads."""
        async with self.lock:
            revision = await self._local_revision()
            if self._result_revision == revision:
                return None
            if self._result_revision is None:
                self._result_revision = revision
                return None
            state = self.store.session(self._day())
            state["day"] = str(self._day())
            return await self._draw(state)

    def _checked(self, action_id, owner_user_id, *, submitted=False):
        self._owner(owner_user_id)
        action = self.store.action(action_id)
        day = date.fromisoformat(action["day"])
        if day != self._day():
            if not self.can_continue(action_id, submitted=submitted):
                raise CardError("Replaced by today's review. Open today's card.")
            day = self._day()
        state = self.store.session(day)
        state["day"] = str(day)
        if (not (submitted and action["spec"].get("form_opened")) and action["status"] not in {"pending", "done"} and action["spec"]["action"] != "return_draft"
                and action["spec"]["screen"] != screen_key(state)):
            raise CardError("The review has moved on. Use the latest buttons.")
        return action, state

    def action_progress(self, action_id, *, owner_user_id, stage="received"):
        """A checked local receipt; never reads providers or confirms a save."""
        action, state = self._checked(action_id, owner_user_id, submitted=True)
        from .render import payload, text
        saves = {'add', 'reuse_addition', 'duplicate_add', 'duplicate_attach', 'followup_disposition',
            'undo_drop', 'note', 'delegate', 'dated_outcome', 'appointment', 'under', 'classify',
            'requirement', 'portion_result', 'portion_select', 'timebox', 'hold', 'planning_cancel',
            'planning_resource', 'planning_portion', 'planning_window', 'planning_shared', 'planning_operation',
            'planning_start', 'correction', 'canceled', 'no_show', 'progress', 'not_done', 'finished',
            'wait', 'arrival', 'done', 'drop', 'save', 'rename', 'parts', 'next', 'change', 'remove_plan',
            'length', 'length_custom', 'date', 'park', 'confirm', 'apply_draft', 'draft_save', 'ask_send', 'ask_words'}
        if action['status'] != 'pending' and action['spec']['action'] not in saves:
            return None
        if action['status'] == 'pending' and stage != 'saving':
            message = "Received. Checking the earlier save before changing anything else."
        elif stage == 'saving':
            message = "Saving your checked answer. Confirmation is still pending."
        else:
            message = "Received. Checking your answer and the latest facts."
        return CardResult(payload([text(message)]), state['day'], state['revision'])

    async def prepare_modal(self, action_id, *, owner_user_id):
        # Read-only local validation must never queue behind a slow record save.
        # handle repeats validation after the transport has acknowledged the form.
        action, state = self._checked(action_id, owner_user_id)
        mark_action(action["spec"]["action"])
        # A recovery button carries the original modal token. Retry its kept
        # checked intent after acknowledgement; do not reopen a pending form.
        if action["status"] in {"pending", "done"}:
            return None
        if not action["spec"].get("modal"):
            return None
        if action["status"] != "ready" or self.store.pending(state["day"]):
            raise CardError("Check the earlier save before opening another answer.")
        self.store.bind_form(action_id)
        form = modal(action_id, action["spec"], date.fromisoformat(state["day"]))
        draft = state.get("submitted_answers", {}).get(_form_key(action["spec"]), state.get("submitted_draft"))
        if (draft and draft.get("submission_id")
                and draft.get("ref") == action["spec"].get("ref")
                and _form_key(dict(draft.get("spec", {}), ref=draft.get("ref"))) == _form_key(action["spec"])):
            for part in form["data"]["components"]:
                field = part.get("component", {})
                value = draft.get(field.get("custom_id"))
                if field.get("type") == 4 and isinstance(value, str):
                    if len(value) <= 4000:
                        # A kept invalid value must remain editable without an
                        # invalid Discord callback or silently truncating it.
                        field["max_length"] = max(field.get("max_length", 4000), len(value))
                        field["value"] = value
                    else:
                        part["description"] = "The earlier answer is kept but exceeds this form's limit. Enter a shorter answer."
                elif field.get("type") in {3, 21}:
                    selected = value if isinstance(value, list) else [value]
                    for option in field.get("options", ()):
                        option["default"] = option["value"] in selected
        return form

    def _queue(self, state, view, stage=None):
        stage = stage or state.get("stage") or state["screen"]
        lines = view["stages"].get(stage, ()) if "stages" in view and stage not in {"catch", "new"} else view["parts"].get(2 if stage == "catch" else 3, ())
        references = [line['reference'] for line in lines]
        snapshot = state.setdefault('queue_snapshots', {}).setdefault(stage, references)
        additions = state.setdefault('queue_additions', {}).setdefault(stage, [])
        additions.extend(ref for ref in references if ref not in snapshot and ref not in additions)
        return [line for line in lines
                if line.get("item_ref") in view["details"]
                and not (work.has_unfinished_prerequisites(line["item_ref"], view["index"])
                         if "index" in view else view["details"][line["item_ref"]].get("prerequisites_unfinished"))]

    def _select(self, state, view):
        if state["screen"] == "answered":
            view.update(item=view["details"].get(state.get("review_ref")), line={}, queue=[])
            return
        if state["screen"] == "selector":
            field, items, page = page_for(state, view)
            view['selector_field'], view['selector_items'], view['selector_page'] = field, items, page
            return
        if state["screen"] == "form_ready":
            card = view['details'].get(state['selector'].get('selected', {}).get('item') or state['selector'].get('source_ref'))
            view.update(item=card, line={}, queue=[])
            return
        if state["screen"] == "waiting_updates":
            view["waiting_items"] = [card for card in view["details"].values()
                if card.get("status") == "waiting" and card.get("arrival_available") and card.get("state") not in {"done", "dropped", "no_longer_needed"}
                and not card.get("prerequisites_unfinished")]
            for card in view['waiting_items']:
                card['wait_context'] = wait_context(card, view.get('index', {}))
            return
        screen = state["screen"]
        if screen == "step_context":
            card = view["details"].get(state.get("steps_ref"))
            view.update(item=card, line=state.get("steps_line", {}), queue=[])
            return
        if state.get("manual_ref") and screen in {"inspect", "update", "item_more", "planning_menu", "when"} and state.get("stage") == "inspect":
            card = view["details"].get(state["manual_ref"])
            if card and card["state"] not in {"done", "dropped", "no_longer_needed"}:
                line = dict(reference="inspect:" + card["ref"], item_ref=card["ref"], kind="manual", synthetic=True,
                            answered=False, offered=(), scope="appointment" if card.get("dates", {}).get("appointment") else self._parent_kind(card["ref"], view))
                state["item"] = line["reference"]
                state["review_ref"] = card["ref"]
                state["review_title"] = card["title"]
                view.update(item=card, line=line, queue=[])
                return
            state.update(screen="day", stage="day", item=None, manual_ref=None)
            return
        if screen not in {"catch", "new", "yesterday", "inbox", "attention", "update", "item_more", "planning_menu", "when", "ask", "item_length", "lengths"}:
            return
        if screen in {"item_length", "lengths"}:
            ref = state.get("length_ref")
            detail = view["details"].get(ref)
            if detail and not detail.get("prerequisites_unfinished") and not detail["length_minutes"] and detail["status"] not in {"ended", "parked"}:
                line = next((line for line in view["parts"].get(3, ()) if line["item_ref"] == ref and line["kind"] == "needs_length" and not line["answered"]), None)
                view.update(item=detail, line=line or {}, queue=[])
                return
            state["screen"] = state.get("stage", "new") if screen == "item_length" else "day"
            screen = state["screen"]
        stage = state.get("stage", "new") if screen in {"when", "ask", "update", "item_more", "planning_menu"} else screen
        state["stage"] = stage
        queue = self._queue(state, view, stage)
        line = next((line for line in queue if line["reference"] == state["item"]), None)
        if line is None and state.get("item"):
            for other_stage in ("yesterday", "inbox", "attention"):
                other_queue = self._queue(state, view, other_stage)
                candidate = next((candidate for candidate in other_queue if candidate["reference"] == state["item"] and not candidate.get("answered")), None)
                if candidate:
                    stage, queue, line = other_stage, other_queue, candidate
                    state["stage"] = stage
                    if screen in {"yesterday", "inbox", "attention"}:
                        state["screen"] = stage
                    break
        if line is None or line.get("answered") or view["details"][line["item_ref"]]["status"] == "ended":
            line = next((line for line in queue if not line["answered"] and line["reference"] not in state["skipped"]
                         and view["details"][line["item_ref"]]["status"] != "ended"), None)
            state["screen"] = stage
        if line is None:
            if stage == "inbox" and state.get("return_to"):
                state.update(state.pop("return_to"))
                return self._select(state, view)
            if stage in {"yesterday", "inbox"}:
                next_stage = "inbox" if stage == "yesterday" else "attention"
                state.update(screen=next_stage, stage=next_stage, item=None)
                return self._select(state, view)
            if stage == "catch":
                state.update(screen="new", stage="new", item=None)
                return self._select(state, view)
            state.update(screen="anything", item=None)
            state.pop('interrupted_review', None)
            return
        state["item"] = line["reference"]
        state["review_ref"] = line["item_ref"]
        state["review_title"] = view["details"][line["item_ref"]]["title"]
        if stage in {'yesterday', 'inbox', 'attention', 'catch', 'new'}:
            state['interrupted_review'] = location(state)
        view.update(line=line, item=view["details"][line["item_ref"]], queue=queue)

    def _candidates(self, state, view):
        candidates = []
        seen = set()
        for line in view["parts"].get(4, ()):
            ref = line.get("item_ref")
            if line.get("kind") == "waiting_context":
                view["waiting"] = line["text"]
            if line.get("kind") != "your_day" or ref not in view["details"] or ref in seen or view["details"][ref].get("prerequisites_unfinished") or view["details"][ref].get("is_up") is False:
                continue
            seen.add(ref)
            card = deepcopy(view["details"][ref])
            scheduled = any(datetime.fromisoformat(slot["start"]).astimezone(self.planning_zone).date().isoformat() == state["day"] for slot in card.get("schedule", ()))
            card["picked"] = state["picks"].get(ref, line.get("already_picked", False) or scheduled)
            if state.get("settling"):
                if ref not in state["settle_eligible"]:
                    continue
                card["picked"] = ref in state["settle_picks"]
            card["line"] = line["reference"]
            candidates.append(card)
        view["candidates"] = candidates

    def _project(self, state, view):
        schedule = view.get('day', {}).get('schedule', {})
        if view.get('full_day', True) and schedule.get('available') and schedule.get('fetched_at'):
            state['last_schedule'] = dict(day=state['day'], fetched_at=schedule['fetched_at'],
                timeline=deepcopy([slot for slot in view['day'].get('timeline', ()) if slot.get('type') == 'task']))
        # Navigation can reuse this tap's snapshot, but never the previous selection.
        for key in ("line", "item", "queue", "waiting", "candidates"):
            view.pop(key, None)
        view['selection_now'] = self.clock()
        self._select(state, view)
        day_screen = state["screen"] in {"open", "day", "settled", "lengths", "planning_details", "day_summary"} and view.get("full_day", True)
        if day_screen:
            self._candidates(state, view)
        if day_screen and "index" in view:
            from .planning import planning_summary
            view["planning"] = planning_summary(view["index"], selected=[c["ref"] for c in view["candidates"] if c["picked"]],
                day_view=view["day"], capacity_minutes=state.get("capacity_minutes"), now=self.clock().astimezone(self.planning_zone))
        return view

    async def _view(self, state, *, opening=None, full_day=None, weekly=None):
        view = await self.backend.fresh(self.clock().astimezone(self.planning_zone) if self.planning_zone else self.clock(), opening=state["screen"] == "open" if opening is None else opening,
            full_day=state["screen"] in {"open", "day", "settled", "lengths", "planning_details", "day_summary"} if full_day is None else full_day,
            weekly=state["screen"] == "weekly" if weekly is None else weekly)
        return self._project(state, view)

    async def _draw(self, state, *, changed=False, view=None):
        now = self.clock()
        if date.fromisoformat(state["day"]) != self._day():
            state["screen"] = "expired"
            return CardResult(render(state, {"day": {}}, lambda *args: "expired"), state["day"], state["revision"])
        if changed and view is None:
            with phase("post_save"):
                view = await self._view(state)
        else:
            view = await self._view(state) if view is None else self._project(state, view)
        needs_day = state["screen"] in {"open", "day", "settled", "lengths", "planning_details", "day_summary"}
        needs_weekly = state["screen"] == "weekly"
        if (needs_day and not view.get("full_day", True)) or (needs_weekly and "weekly" not in view):
            if "_read_session" in view:
                view = await self.backend.extend(view, full_day=needs_day, weekly=needs_weekly)
                view = self._project(state, view)
            else:
                view = await self._view(state)
        draft = None
        line = view.get("line") or {}
        ask_available = bool(self.diablo and self.settings.ask_diablo and line.get("reference")
            and not line.get("synthetic") and not line.get("answered") and state["screen"] != "answered")
        if ask_available and view.get("item"):
            payload = self._diablo_context(state, view)
            item = self._authorized_item(self.settings.owner_id, payload["day"], payload["line_ref"], payload["item_ref"], view)
            draft = await self.diablo.read(payload, owner_id=self.settings.owner_id, authorized_item=item)
        state["revision"] += 1
        if draft and draft.get("state") == "draft":
            version = [draft.get("operation_id"), draft.get("draft_sha256")]
            if state.get("draft_preview_version") != version:
                state.update(draft_preview_version=version, draft_page=0)
        def issue(label, action, options):
            spec = dict(options, label=label, action=action, screen=screen_key(state), timezone=state.get("timezone", "Asia/Singapore"))
            card = view.get("item")
            if options.get("ref"):
                card = view["details"].get(options["ref"])
            if card:
                spec.update(ref=card["ref"], expected={"record_id": card["record_id"], "sha256": card["record_sha256"]})
                line = view.get("line") or {}
                # Informational questions have no mutation answers. Choosing
                # a date is a fresh, proof-bound owner instruction instead.
                spec["line"] = None if line.get("synthetic") or (action == "date" and line.get("kind") == "new_nothing_needed") else line.get("reference")
                spec["question"] = line.get("reference")
                spec["outcome_day"] = line.get("commitment_day") or state["day"]
                spec["scope"] = line.get("scope")
                spec["planning"] = card.get("planning", {})
                spec["timezone"] = state.get("timezone", "Asia/Singapore")
                spec["suggested_title"] = line.get("suggested_title") or line.get("suggested_name")
                spec["suggested_label"] = line.get("suggested_label")
                spec["at"] = card.get("dates", {}).get("appointment", {}).get("at")
            if action == "confirm":
                spec["approval_sha256"] = state.get("day_approval", {}).get("sha256")
            if action in PICKER_ACTIONS and options.get('modal') and not options.get('selection_bound'):
                spec.update(action='begin_selector', selected_action=action, modal=False)
            if action == 'bound_form':
                picker = state['selector']
                selected = picker['selected']
                target_ref = selected.get('item') or picker.get('source_ref')
                target = view['details'].get(target_ref)
                spec.update(picker['spec'], label=label, screen=screen_key(state), action=picker['action'], modal=True,
                            selection_bound=True, selected_fields=deepcopy(selected), context_expected=deepcopy(picker['proofs']))
                if target:
                    spec.update(ref=target_ref, expected=proof(target), planning=target.get('planning', {}), line=None, question=None)
            return self.store.issue(state["day"], spec)
        if state["screen"] == "day_summary" and not state.get("day_approval"):
            from .planning import day_approval
            state["day_approval"] = day_approval(view, state)
        message = render(state, view, issue, emojis=self.emojis, dashboard_url=self.settings.dashboard_url,
                         ask=ask_available, draft=draft, pending=self.store.pending(state["day"]))
        self.store.save(state["day"], state)
        self._result_revision = await self._local_revision()
        return CardResult(message, state["day"], state["revision"], changed=changed)

    def _diablo_context(self, state, view):
        return dict(day=state["day"], line_ref=view["line"]["reference"], item_ref=view["item"]["ref"])

    async def authorize_item(self, owner_id, day, line_ref, item_ref):
        if owner_id != self.settings.owner_id or day != str(self._day()):
            raise CardError("That review is no longer available.")
        state = self.store.session(day)
        state["day"] = day
        view = await self.backend.fresh(self.clock(), full_day=False)
        return self._authorized_item(owner_id, day, line_ref, item_ref, view)

    def _authorized_item(self, owner_id, day, line_ref, item_ref, view):
        if owner_id != self.settings.owner_id or day != str(self._day()):
            raise CardError("That review is no longer available.")
        line = next((line for part in (2, 3) for line in view["parts"].get(part, ())
                     if line["reference"] == line_ref and line["item_ref"] == item_ref and not line["answered"]), None)
        card = view["details"].get(item_ref)
        if not line or not card or card["status"] == "ended" or card.get("prerequisites_unfinished"):
            raise CardError("That question has changed. Open the latest item.")
        return {**card, "offered": line["offered"]}

    def current_diablo_binding(self):
        """Read presentation association only; authorize_item verifies it before use."""
        day = str(self._day())
        state = self.store.session(day)
        binding = state.get("diablo_binding")
        return dict(binding) if binding else None

    async def draft_control(self, binding):
        """Issue a thread's exact-item return control without changing the active form."""
        await self.authorize_item(self.settings.owner_id, binding["day"], binding["line_ref"], binding["item_ref"])
        token = self.store.issue(binding["day"], dict(action="return_draft", label="Open this draft",
            screen=None, binding=dict(binding)))
        return {"type": 2, "style": 2, "label": "Open this draft", "custom_id": token}

    async def handle(self, action_id, interaction_id, fields=None, *, owner_user_id=None, progress=None):
        """Transport calls only after a successful Discord acknowledgement."""
        async with self.lock:
            action, state = self._checked(action_id, owner_user_id, submitted=bool(fields))
            if fields and action["spec"].get("form_opened"):
                state["submitted_draft"] = {**fields, "submission_id": interaction_id, "ref": action["spec"].get("ref"), "spec": action["spec"]}
                state.setdefault("submitted_answers", {})[_form_key(action["spec"])] = deepcopy(state["submitted_draft"])
                self.store.save(state["day"], state)
            if not isinstance(interaction_id, str) or not interaction_id or len(interaction_id) > 128:
                raise CardError("This tap could not be checked. Try again.")
            state["error"] = ""
            if action["status"] == "done":
                return await self._draw(state)
            if action["status"] == "refused":
                state["error"] = action["result"]["message"]
                return await self._draw(state)
            spec = action["spec"]
            kind = spec["action"]
            if fields and spec.get("form_opened"):
                spec = dict(spec, submission_id=interaction_id)
            mark_action(kind)
            if spec.get("modal") and not fields and action["status"] == "ready":
                raise CardError("Open the answer box before saving.")
            if self.store.pending(state["day"]) and action["status"] != "pending":
                return await self._draw(state)
            if kind in {'return_change', 'return_transfer', 'return_discharge', 'return_reason', 'return_preview_page'}:
                view = await self._view(state, opening=False, full_day=False)
                if kind == 'return_change':
                    state['history'].append(location(state))
                    state['selector'] = dict(action='return_change', spec={}, source_ref=None,
                        fields=['source'], field_index=0, offset=0, selected={}, proofs={})
                    state.update(screen='selector', page=0)
                elif kind == 'return_transfer':
                    change = state['return_change']
                    state['history'].append(location(state))
                    state['selector'] = dict(action='return_replacement', spec={}, source_ref=change['source'],
                        fields=['to_ref'], field_index=0, offset=0, selected={}, proofs={})
                    state.update(screen='selector', page=0)
                elif kind == 'return_discharge':
                    state['return_change'].update(kind='no_longer_required')
                    state.update(screen='return_change_form')
                elif kind == 'return_preview_page':
                    state['return_preview_page'] = spec['value']
                else:
                    change = state['return_change']
                    bound = {'source':change['source'], 'from_ref':change['from_ref'], 'to_ref':change.get('to_ref')}
                    if spec.get('selected_fields') != bound or spec.get('kind') != change['kind']:
                        raise CardError('This form belongs to an earlier return choice. Reopen the current form; nothing changed.')
                    reason = fields.get('reason', '').strip()
                    if not reason or len(reason) > 1024:
                        raise CardError('Explain why this required return is changing. Nothing changed.')
                    disposition = dict(kind=change['kind'], from_ref=change['from_ref'], reason=reason)
                    if change['kind'] == 'transfer':
                        assignee = fields.get('assignee', '').strip()
                        if not assignee or len(assignee) > 128 or fields.get('agreed') != 'agreed':
                            state['error'] = 'Confirm the named person has agreed to this responsibility. Nothing changed.'
                            return await self._draw(state, view=view)
                        disposition.update(to_ref=change['to_ref'], at=change['at'], assignee=assignee, approved=True)
                    change['disposition'] = disposition
                    state.update(screen='return_preview', return_preview_page=0)
                    state.pop('submitted_draft', None)
                self.store.finish(action_id, {'navigation': True}, state)
                return await self._draw(state, view=view)
            if kind in {'begin_selector', 'selector_page', 'selector_choose', 'selector_continue', 'selector_none'}:
                view = await self._view(state, opening=False, full_day=kind == 'begin_selector' and spec.get('selected_action') == 'portion_select')
                if kind == 'begin_selector':
                    state['history'].append(location(state))
                    selected_action = spec['selected_action']
                    source_ref = spec.get('ref')
                    if selected_action in {'inspect', 'correction', 'timebox'} or state['screen'] in {'day', 'day_more', 'settled'}:
                        source_ref = None
                    state['selector'] = dict(action=selected_action, spec=deepcopy(spec), source_ref=source_ref,
                        fields=fields_for(selected_action, source_ref), field_index=0, offset=0, selected={},
                        proofs={source_ref: spec['expected']} if source_ref and spec.get('expected') else {},
                        duplicate_refs=state.get('duplicate_matches', [{}])[0].get('refs', ()) if selected_action == 'duplicate_choose' else ())
                    state.update(screen='selector', page=0)
                else:
                    picker = state.get('selector')
                    if not picker or state['screen'] != 'selector':
                        raise CardError('Open the latest choices. Nothing changed.')
                    if kind == 'selector_page':
                        picker['offset'] = spec['offset']
                    elif kind == 'selector_none':
                        if spec['field'] != 'add':
                            raise CardError('That choice is not optional. Nothing changed.')
                        picker['selected']['add'] = ''
                        self._advance_selector(state, view)
                    elif kind == 'selector_continue':
                        field = picker['fields'][picker['field_index']]
                        if not picker['selected'].get(field):
                            state['error'] = 'Choose at least one item, or use Back to cancel.'
                        else:
                            self._advance_selector(state, view)
                    else:
                        values = (fields or {}).get('values')
                        if not isinstance(values, list) or (not values and spec['field'] not in {'tasks', 'ids'}) or len(values) > 25 or any(value not in spec['allowed'] for value in values):
                            raise CardError('Choose from the current list. Nothing changed.')
                        field = picker['fields'][picker['field_index']]
                        current_field, all_items, page = page_for(state, view)
                        if field != spec['field'] or current_field != field:
                            raise CardError('The choices moved on. Nothing changed.')
                        current = {item['ref']: item for item in all_items}
                        chosen = [spec['allowed'][value] for value in values]
                        if any(item['ref'] not in current or current[item['ref']]['proof'] != item['proof'] for item in chosen):
                            state['error'] = 'An item changed. Check the fresh choices; nothing changed.'
                            return await self._draw(state, view=view)
                        for item in chosen:
                            if item['proof']:
                                picker['proofs'][item['ref']] = item['proof']
                        if field in {'tasks', 'ids'}:
                            on_page = {item['ref'] for item in spec['allowed'].values()}
                            before = picker['selected'].get(field, [])
                            picker['selected'][field] = [ref for ref in before if ref not in on_page] + [item['ref'] for item in chosen]
                        else:
                            if len(chosen) != 1:
                                raise CardError('Choose one item. Nothing changed.')
                            picker['selected'][field] = chosen[0]['ref']
                            self._advance_selector(state, view)
                self.store.finish(action_id, {'navigation': True}, state)
                return await self._draw(state, view=view)
            if kind in {"waiting_item", "inspect_saved"}:
                view = await self._view(state, opening=False, full_day=False)
                card = view["details"].get(spec.get("ref"))
                if not card or card.get("state") in {"done", "dropped", "no_longer_needed"} or card.get("prerequisites_unfinished") or kind == "waiting_item" and (card.get("status") != "waiting" or not card.get("arrival_available")):
                    state["error"] = "This item is no longer available here. Nothing changed."
                    return await self._draw(state, view=view)
                state["history"].append(location(state))
                state.update(screen="inspect", stage="inspect", manual_ref=card["ref"], item=None)
                self.store.finish(action_id, {"navigation": True}, state)
                return await self._draw(state, view=view)
            if kind == "inspect":
                view = await self._view(state, opening=False, full_day=False)
                value = words(fields, limit=256)
                matches = [card for card in view["details"].values() if value in {card["ref"],card["title"]} and card["state"] not in {"done", "dropped", "no_longer_needed"} and not card.get("prerequisites_unfinished")]
                if len(matches) != 1:
                    state["error"] = "Choose one exact current item title or ID. Nothing changed."
                    return await self._draw(state)
                state["history"].append(location(state))
                state.update(screen="inspect", stage="inspect", manual_ref=matches[0]["ref"], item=None)
                self.store.finish(action_id, {"navigation": True}, state)
                return await self._draw(state, view=view)
            if kind == "capacity":
                value = words(fields, limit=10).strip()
                try:
                    hours = float(value)
                    if not 0 <= hours <= 24:
                        raise ValueError
                except ValueError:
                    state["error"] = "Choose available hours from 0 to 24. Your fixed commitments stay in the day."
                    return await self._draw(state)
                state["capacity_minutes"] = round(hours * 60)
                self.store.finish(action_id, {"navigation": True}, state)
                return await self._draw(state)
            if kind == "timezone":
                value = words(fields, limit=100).strip()
                matches = [name for name in available_timezones() if name.casefold() == value.casefold()
                           or name.rsplit("/", 1)[-1].replace("_", " ").casefold() == value.casefold()]
                if len(matches) != 1:
                    state["error"] = "Choose an unambiguous IANA zone, such as Australia/Sydney. Your local date is unchanged."
                    return await self._draw(state)
                self.planning_zone = ZoneInfo(matches[0])
                old_day = state["day"]
                state["timezone"] = matches[0]
                new_day = str(self._day())
                if new_day != old_day:
                    # Carry local choices, never pending mutation identity, into
                    # the destination day. Canonical facts are re-read on draw.
                    carried = dict(state["picks"])
                    self.store.save(old_day, state)
                    state = self.store.session(new_day)
                    state.update(day=new_day, timezone=matches[0], picks=carried,
                                 settling=False, date_changed=True, screen="day", stage="day", page=0)
                state.pop("submitted_draft", None)
                self.store.save(new_day, state)
                self.store.finish(action_id, {"navigation": True}, state)
                return await self._draw(state)
            # Navigation needs current records/proofs first, destination outputs later.
            # Toggle and confirmation still need the current day candidates.
            narrow_navigation = kind in {"start", "go_day", "back", "skip", "not_yet", "when", "ask",
                "more", "first", "refresh", "weekly", "weekly_next", "update", "planning_menu",
                "planning_details", "resume_review", "waiting_updates", "return_draft", "review_addition"}
            view = await self._view(state, opening=False,
                full_day=False if narrow_navigation else None, weekly=False if narrow_navigation else None)
            if kind in {"saved_page", "schedule_page", "schedule_only"}:
                if kind in {'saved_page', 'schedule_page'}:
                    state[kind] = spec['value']
                self.store.finish(action_id, {"navigation": True}, state)
                return await self._draw(state, view=view)
            if kind == "answer_page":
                state["page"] = spec["value"]
                self.store.finish(action_id, {"navigation": True}, state)
                return await self._draw(state, view=view)
            if kind in {"review_choices", "summary_page", "length_unknown"}:
                if kind == "review_choices":
                    state["history"].append(location(state))
                    state.update(screen="day_summary", stage="day", item=None, page=0)
                    state.pop("day_approval", None)
                elif kind == "summary_page":
                    state["page"] = spec["value"]
                else:
                    state["unknown_estimates"] = list(dict.fromkeys([*state.get("unknown_estimates", []), spec["ref"]]))
                    if not state.get("settling"):
                        state.update(screen=state.get("stage", "inbox"), item=None)
                self.store.finish(action_id, {"navigation": True}, state)
                if kind == "length_unknown" and state.get("settling"):
                    return await self._finish_lengths(state)
                return await self._draw(state, view=view)
            if kind == "review_addition":
                draft = state.get("submitted_draft", {})
                if draft.get("spec", {}).get("action") != "add":
                    raise CardError("That kept addition is no longer available. Nothing changed.")
                words(draft)
                state["history"].append(location(state))
                state.update(screen="kept_addition", kept_addition_id=draft.get("submission_id"))
                self.store.finish(action_id, {"navigation": True}, state)
                return await self._draw(state, view=view)
            if kind == "reuse_addition" and action["status"] == "ready":
                draft = state.get("submitted_draft", {})
                if draft.get("spec", {}).get("action") != "add" or draft.get("submission_id") != state.get("kept_addition_id"):
                    state["error"] = "The kept answer changed. Review its latest words before saving. Nothing changed."
                    return await self._draw(state, view=view)
                fields = {"value": words(draft), "order": draft.get("order", "independent")}
                spec = dict(spec, action="add", ref=None, expected=None, line=None, question=None, submission_id=draft.get("submission_id"))
                kind = "add"
            if kind == "add" and action["status"] == "ready" and fields:
                parts = capture_parts(words(fields), fields.get("order", "independent"))
                matches = []
                for number, part in enumerate(parts):
                    found = [card for card in view["details"].values() if card["title"].casefold() == part["title"].casefold()]
                    if found:
                        matches.append({"part": number, "refs": [card["ref"] for card in found], "expected": {card["ref"]: {"record_id": card["record_id"], "sha256": card["record_sha256"]} for card in found}})
                if matches:
                    state.update(screen="duplicate", duplicate_capture=dict(fields), capture_parts=parts, duplicate_matches=matches,
                                 duplicate_ref=matches[0]["refs"][0] if len(matches[0]["refs"]) == 1 else None, duplicate_part=matches[0]["part"], capture_expected={}, capture_submission_id=spec.get("submission_id"))
                    self.store.finish(action_id, {"navigation": True}, state)
                    return await self._draw(state, view=view)
            if kind in {"duplicate_add", "duplicate_attach", "duplicate_choose"}:
                if kind == "duplicate_choose":
                    value = words(fields, limit=256)
                    candidates = state["duplicate_matches"][0]["refs"]
                    chosen = [ref for ref in candidates if value in {ref, view["details"][ref]["title"]}]
                    if len(chosen) != 1:
                        state["error"] = "Choose one exact existing item ID; the names match more than one item. Nothing changed."
                        return await self._draw(state, view=view)
                    state["duplicate_ref"] = chosen[0]
                fields = state.get("duplicate_capture", {})
                parts = state.get("capture_parts", [])
                if kind in {"duplicate_attach", "duplicate_choose"}:
                    ref = state["duplicate_ref"]
                    card = view["details"].get(ref)
                    if not card or card["state"] in {"done", "dropped", "no_longer_needed"}:
                        state["error"] = "This item is closed. Keep the capture separate instead. Nothing changed."
                        return await self._draw(state, view=view)
                    proof = spec.get("expected") if spec.get("ref") == ref else state["duplicate_matches"][0].get("expected", {}).get(ref)
                    if proof and proof != {"record_id": card["record_id"], "sha256": card["record_sha256"]}:
                        state["duplicate_matches"][0].setdefault("expected", {})[ref] = {"record_id": card["record_id"], "sha256": card["record_sha256"]}
                        state["error"] = "This item changed since the preview. Check it and choose again. Nothing changed."
                        return await self._draw(state, view=view)
                    parts[state["duplicate_part"]]["existing_ref"] = ref
                    state["capture_expected"][ref] = spec.get("expected") if spec.get("ref") == ref else {"record_id": card["record_id"], "sha256": card["record_sha256"]}
                remaining = state["duplicate_matches"][1:]
                state["duplicate_matches"] = remaining
                if remaining:
                    state.update(duplicate_ref=remaining[0]["refs"][0] if len(remaining[0]["refs"]) == 1 else None, duplicate_part=remaining[0]["part"])
                    self.store.finish(action_id, {"navigation": True}, state)
                    return await self._draw(state, view=view)
                capture_expected = state["capture_expected"]
                state.update(screen="inbox", stage="inbox", item=None)
                spec = dict(spec, ref=None, screen=screen_key(state), action="add", label="Save captured steps", capture_parts=parts, capture_expected=capture_expected, submission_id=state.get("capture_submission_id"))
                kind = "add"
            if kind == "return_draft":
                binding = spec["binding"]
                await self.authorize_item(self.settings.owner_id, binding["day"], binding["line_ref"], binding["item_ref"])
                stage = next((stage for stage, lines in view.get("stages", {}).items() if any(line["reference"] == binding["line_ref"] for line in lines)), None)
                if stage is None:
                    part = next(part for part in (2, 3) if any(line["reference"] == binding["line_ref"] for line in view["parts"].get(part, ())))
                    stage = "catch" if part == 2 else "new"
                state["history"].append(location(state))
                state.update(screen=stage, stage=stage, item=binding["line_ref"])
                self.store.finish(action_id, {"navigation": True}, state)
                return await self._draw(state)
            if kind == "existing_steps":
                if state["screen"] != "step_context":
                    state["history"].append(location(state))
                    state["steps_line"] = dict(view.get("line") or {})
                state.update(screen="step_context", steps_ref=spec["ref"], steps_page=spec.get("value", 0))
                self.store.finish(action_id, {"navigation": True}, state)
                return await self._draw(state, view=view)
            if kind == "draft_page":
                draft = await self.diablo.read(self._diablo_context(state, view), owner_id=self.settings.owner_id)
                if draft.get("state") != "draft" or draft.get("draft_sha256") != spec.get("draft_sha256") or draft.get("operation_id") != spec.get("draft_operation_id"):
                    state["error"] = "The draft changed. Read its latest complete preview before saving. Nothing changed."
                else:
                    state["draft_page"] = spec["value"]
                self.store.finish(action_id, {"navigation": True}, state)
                return await self._draw(state, view=view)
            current = view.get("item")
            old_form = fields and spec.get("form_opened") and (spec["screen"] != screen_key(state) or action["day"] != state["day"])
            if old_form and (not current or current["ref"] != spec.get("ref") or current["record_sha256"] != spec.get("expected", {}).get("sha256")
                             or view.get("line", {}).get("reference") != spec.get("question")):
                state["error"] = "The date or item changed. Your submitted words are kept; check the current question before saving them."
                return await self._draw(state)
            if action["status"] != "pending" and not old_form and spec["screen"] != screen_key(state):
                state["error"] = "That item has changed. Check the fresh question before answering."
                return await self._draw(state)
            if kind in {"start", "go_day", "back", "skip", "not_yet", "when", "ask", "more", "first", "toggle", "refresh", "weekly", "weekly_next", "update", "planning_menu", "planning_details", "resume_review", "waiting_updates", "item_more", "day_more", "open_more"}:
                self._navigate(state, view, spec)
                self.store.finish(action_id, {"navigation": True}, state)
                return await self._draw(state, view=view)
            if kind == "confirm" and action["status"] == "ready":
                from .planning import day_approval
                current = day_approval(view, state)
                approved = state.get("day_approval")
                if state["screen"] != "day_summary" or not approved or spec.get("approval_sha256") != approved["sha256"]:
                    state.update(screen="day_summary", stage="day", item=None, page=0,
                                 error="Review the complete choices before saving. Nothing changed.")
                    state["day_approval"] = current
                    return await self._draw(state, view=view)
                if current["sha256"] != approved["sha256"]:
                    state["day_approval"] = current
                    state.update(page=0, error="Your choices or a source fact changed. Review this updated summary before saving. Nothing changed.")
                    return await self._draw(state, view=view)
                selected = [card for card in view["candidates"] if card["picked"]]
                missing = [card for card in selected if not card["length_minutes"] and card["ref"] not in state.get("unknown_estimates", ())]
                if missing:
                    state["picks"].update({card["ref"]: card["picked"] for card in view["candidates"]})
                    state.update(screen="lengths", stage="day", length_ref=missing[0]["ref"], settling=True,
                                 settle_eligible=[card["ref"] for card in view["candidates"]],
                                 settle_picks=[card["ref"] for card in selected],
                                 settle_key="review-card-" + hashlib.sha256(action_id.encode()).hexdigest()[:40])
                    self.store.finish(action_id, {"navigation": True}, state)
                    return await self._draw(state, view=view)
            operation = "review-card-" + hashlib.sha256(action_id.encode()).hexdigest()[:40]
            retrying = action["status"] == "pending"
            if action["status"] == "ready":
                try:
                    intent = self._intent(operation, state, view, spec, fields or {})
                    if kind == "draft_save":
                        prepared = await self.diablo.prepare_save(intent["payload"], owner_id=self.settings.owner_id)
                        intent["request"] = request_data(prepared)
                except (CardError, DiabloRefused, ValueError) as exc:
                    state["error"] = str(exc)
                    return await self._draw(state)
                action = self.store.prepare(action_id, intent)
            intent = action["intent"]
            if progress:
                await progress('checking_earlier' if retrying else 'saving')
            try:
                with phase("confirm_change"):
                    result = await self._execute(intent)
            except BrainRuntimeError as exc:
                message = exc.message + (" " + exc.fix if exc.fix else "")
                if exc.retryable:
                    state["error"] = "Couldn't confirm the save. Check it again before changing anything else."
                else:
                    self.store.refused(action_id, {"message": message})
                    state["error"] = message
                self.store.save(state["day"], state)
                return await self._draw(state)
            except (CardError, DiabloRefused) as exc:
                self.store.refused(action_id, {"message": str(exc)})
                state["error"] = str(exc)
                return await self._draw(state)
            except Exception:
                state["error"] = "Couldn't confirm the save. Check it again before changing anything else."
                self.store.save(state["day"], state)
                return await self._draw(state)
            if result.get("status") not in {"accepted", "duplicate"} or result.get("remote_observed") is not True:
                state["error"] = "The save was not confirmed. Check it again before changing anything else."
                return await self._draw(state)
            latest_draft = state.get("submitted_draft")
            answers = state.get("submitted_answers", {})
            state = deepcopy(intent["after"])
            state["submitted_answers"] = {key: value for key, value in answers.items()
                if value.get("submission_id") != intent.get("consumed_draft")}
            if latest_draft and latest_draft.get("submission_id") != intent.get("consumed_draft"):
                state["submitted_draft"] = latest_draft
            else:
                state.pop("submitted_draft", None)
            if kind == 'confirm':
                state['saved_choices'] = deepcopy(intent['approval'])
                state['saved_choices']['confirmed_at'] = self.clock().isoformat()
                state['saved_page'] = 0
            state["day"] = str(self._day())
            if intent["day"] != state["day"]:
                state.update(date_changed=True, settling=False)
                if kind == "confirm":
                    state.update(screen="day", stage="day", item=None)
            state.pop("duplicate_capture", None)
            if kind == "add":
                state["added"] = list(dict.fromkeys([*state["added"], *result.get("created_refs", ())]))
            if kind == "add" and self.auto_tidy and self.settings.auto_tidy:
                # The hook owns its durable queue and returns after enqueue, not a model round trip.
                try:
                    await self.auto_tidy(operation, tuple(result.get("created_refs", ())))
                    state["tidying"] = True
                except Exception:
                    state["error"] = "Your new items are saved. Diablo couldn't tidy them just now."
                self.store.save(state["day"], state)
            # Finish only after the idempotent enqueue hook: a crash cannot
            # acknowledge the batch and silently forget its automatic tidy.
            self.store.finish(action_id, result, state)
            if kind in {"length", "length_custom"} and state.get("settling"):
                return await self._finish_lengths(state)
            return await self._draw(state, changed=True)

    async def _finish_lengths(self, state):
        view = await self._view(state)
        if not set(state["settle_picks"]) <= {card["ref"] for card in view["candidates"]}:
            state.update(screen="day", settling=False,
                         error="Your estimate was saved. Today's choices changed; review them before saving your day.")
            state.pop("day_approval", None)
            return await self._draw(state, changed=True)
        missing = [card for card in view["candidates"] if card["picked"] and not card["length_minutes"]
                   and card["ref"] not in state.get("unknown_estimates", ())]
        if missing:
            state.update(screen="lengths", stage="day", length_ref=missing[0]["ref"])
        else:
            state.update(screen="day_summary", stage="day", item=None, page=0,
                         win="Estimates reviewed. Check the full choices, then save explicitly.")
            state.pop("day_approval", None)
        return await self._draw(state, changed=True)

    def _navigate(self, state, view, spec):
        kind = spec["action"]
        if kind == "resume_review":
            state.update(state.pop("review_resume", state.get('interrupted_review') or dict(screen="day", stage="day", item=None)))
            return
        if kind == "back":
            resume = location(state)
            previous = state["history"].pop() if state["history"] else dict(screen="open", item=None, mode="planned", not_yet=False, page=0)
            if previous.get("screen") in {"yesterday", "inbox", "attention", "catch", "new", "update", "planning_menu", "when", "ask", "item_length"} and previous.get("review_ref"):
                queue = self._queue(state, view, previous.get("stage") or previous["screen"])
                line = next((line for line in queue if line["reference"] == previous.get("item")), None)
                manual = previous.get("stage") == "inspect"
                card = view["details"].get(previous.get("review_ref"))
                unavailable = not card or card.get("status") == "ended" or card.get("prerequisites_unfinished")
                if unavailable or not manual and (line is None or line.get("answered")):
                    previous.update(screen="answered")
                    if resume.get("screen") != "answered":
                        state["review_resume"] = resume
            state.update(previous)
            state["settling"] = False
            return
        state["history"].append(location(state))
        state["history"] = state["history"][-100:]
        if kind == "waiting_updates":
            state.update(screen="waiting_updates", stage="waiting_updates", item=None, page=0)
        elif kind == "start":
            state.update(screen="yesterday" if "stages" in view else "catch", stage="yesterday" if "stages" in view else "catch", item=None)
        elif kind == "go_day":
            state.update(screen="day", stage="day", item=None, page=0, day_offset=0, settling=False)
            state.pop("day_approval", None)
        elif kind == "skip" and state.get("stage") == "inspect":
            state.update(screen="day", stage="day", item=None, manual_ref=None)
        elif kind == "skip":
            state["skipped"].append(state["item"])
            state.update(item=None, not_yet=False)
        elif kind == "not_yet":
            state["not_yet"] = True
        elif kind == "when":
            state.update(screen="when", mode=spec["mode"])
        elif kind == "planning_details":
            state.update(screen="planning_details", page=0)
        elif kind in {"update", "item_more", "day_more", "open_more", "planning_menu"}:
            state["screen"] = kind
            if kind == "planning_menu":
                state["page"] = 1 if spec.get("value") == "more" else 0
        elif kind == "ask":
            state["screen"] = "ask"
        elif kind == "toggle":
            card = next((card for card in view["candidates"] if card["ref"] == spec["ref"]), None)
            if card is None:
                raise CardError("That thing is no longer available to pick.")
            state["picks"][card["ref"]] = not card["picked"]
        elif kind in {"more", "first"}:
            if state["screen"] == "day":
                state["day_offset"] = spec.get("next_offset", state.get("day_offset", 0) + 5) if kind == "more" else 0
            state["page"] = state["page"] + 1 if kind == "more" else 0
        elif kind == "weekly":
            state.update(screen="weekly", page=0)
        elif kind == "weekly_next":
            state["page"] = state["page"] + 1

    def _advance_selector(self, state, view):
        picker = state['selector']
        picker['field_index'] += 1
        picker['offset'] = 0
        if picker['field_index'] < len(picker['fields']):
            return
        if picker['action'] == 'duplicate_choose':
            state.update(screen='duplicate', duplicate_ref=picker['selected']['item'])
            state.pop('selector', None)
        elif picker['action'] == 'return_change':
            from brain_core.planning import required_followup
            ref = picker['selected']['source']
            record = view['index'][ref]
            followup = required_followup(record, view['index'])
            expected = dict(picker['proofs'])
            target = view['details'].get(followup['item_ref'])
            if target:
                expected[followup['item_ref']] = proof(target)
            original = record.data['planning']['operation']['followup_ref']
            original_card = view['details'].get(original)
            if original_card:
                expected[original] = proof(original_card)
            state['return_change'] = dict(source=ref, from_ref=followup['item_ref'], at=followup['at'],
                expected=expected, source_title=record.title,
                original_title=view['details'].get(original, {}).get('title', 'Unavailable original return'),
                active_title=(target or {}).get('title', 'Unavailable required return'))
            state.update(screen='return_change')
            state.pop('selector', None)
        elif picker['action'] == 'return_replacement':
            ref = picker['selected']['to_ref']
            state['return_change'].update(kind='transfer', to_ref=ref, replacement_title=view['details'][ref]['title'])
            state['return_change']['expected'].update(picker['proofs'])
            state.update(screen='return_change_form')
            state.pop('selector', None)
        elif picker['action'] == 'inspect':
            ref = picker['selected']['item']
            state.update(screen='inspect', stage='inspect', manual_ref=ref, item=None)
            state.pop('selector', None)
        else:
            state['screen'] = 'form_ready'

    @staticmethod
    def _parent_kind(ref, view):
        from brain_core.work import kind_of
        return kind_of(ref, view["index"])

    def _intent(self, operation, state, view, spec, fields):
        if spec.get('selection_bound'):
            selected_fields = spec.get('selected_fields', {})
            if any(key in fields and fields[key] != value for key,value in selected_fields.items()):
                raise CardError('Use the bound choices; changed context cannot be supplied in the form. Nothing changed.')
            fields = {**fields, **selected_fields}
            for ref, expected in spec.get('context_expected', {}).items():
                card = view['details'].get(ref)
                if not card or proof(card) != expected:
                    raise CardError('A chosen item changed. Choose again before saving. Nothing changed.')
        after = deepcopy(state)
        after["error"] = ""
        after["history"].append(location(state))
        kind = spec["action"]
        intent = dict(operation=operation, action=kind, owner_id=self.settings.owner_id, after=after,
                      at=self.clock().isoformat(), day=state["day"], consumed_draft=spec.get("submission_id"))
        if kind in {"ask_send", "ask_words", "draft_save"}:
            if not self.diablo or not self.settings.ask_diablo:
                raise CardError("Ask Diablo is switched off.")
            payload = dict(operation_id=operation, day=state["day"], line_ref=spec["line"], item_ref=spec["ref"])
            if kind == "draft_save":
                payload.update(draft_operation_id=spec["draft_operation_id"], draft_sha256=spec["draft_sha256"])
                after.update(screen=state.get("stage", "new"), item=None, win="Steps saved")
            else:
                payload.update(expected=spec["expected"], action=spec.get("value", "words"), text=words(fields) if kind == "ask_words" else "")
                after.update(screen=state.get("stage", "new"), win="")
                after["diablo_binding"] = {key: payload[key] for key in ("operation_id", "day", "line_ref", "item_ref")}
            intent["payload"] = payload
            return intent
        if kind == 'followup_disposition':
            change = state.get('return_change', {})
            if spec.get('ref') != change.get('source') or spec['disposition'] != change.get('disposition') or spec['disposition_expected'] != change.get('expected'):
                raise CardError('The preview changed. Approve only the current complete preview; nothing changed.')
            for ref, expected in spec['disposition_expected'].items():
                card = view['details'].get(ref)
                if not card or proof(card) != expected:
                    raise CardError('A required return or chosen task changed. Start the change again; nothing changed.')
            intent['request'] = request_data(make_request(operation, spec, {}, date.fromisoformat(state['day'])))
            after.update(screen='planning_details', page=0, win='Required return change saved')
            after.pop('return_change', None)
            after['history'] = [entry for entry in after['history'] if entry.get('screen') not in {'return_change', 'return_change_form', 'return_preview', 'selector'}]
            while after['history'] and after['history'][-1].get('screen') == 'planning_details':
                after['history'].pop()
            return intent
        if kind == "confirm":
            selected = [card for card in view["candidates"] if card["picked"]]
            moves, expected = [], deepcopy(state["day_approval"]["proofs"])
            for card in view["candidates"]:
                if card["picked"]:
                    moves.append({"move": "date", "thing": card["ref"], "when": state["day"], "kind": "planned", "unpark": True})
                else:
                    continue
                expected[card["ref"]] = dict(record_id=card["record_id"], sha256=card["record_sha256"])
            if moves:
                intent["request"] = request_data(WorkRequest(operation, "Yes, that's my day \u00b7 " + state["day"], "owner", "direct", tuple(moves), expected_records=expected))
            intent["approval"] = deepcopy(state["day_approval"])
            intent["picks"] = [card["ref"] for card in selected]
            intent["eligible"] = [card["ref"] for card in view["candidates"]]
            after.update(screen="settled", item=None, settling=False, provisional=bool(view.get("planning", {}).get("provisional") or view.get("planning", {}).get("issues")))
            return intent
        if not spec.get('selection_bound') and (kind in {"correction", "timebox", "requirement"} or kind in {"portion_select", "portion_result"} and fields.get("item")):

            value = fields.get("item", "")
            matches = [card for card in view["details"].values() if value in {card["ref"], card["title"]} and (card["state"] == "done" if kind == "correction" else card.get("is_up") if kind == "timebox" else True)]
            if len(matches) != 1:
                raise CardError("Choose one exact completed item title or ID; nothing changed.")
            card = matches[0]
            spec = dict(spec, planning=card.get("planning", {}), ref=card["ref"], expected={"record_id": card["record_id"], "sha256": card["record_sha256"]}, line=None)
        if kind == "apply_draft":
            draft = state.get("submitted_draft")
            if not draft or not view.get("item") or draft.get("ref") != view["item"]["ref"]:
                raise CardError("Open the kept answer's current item before applying it.")
            original = draft.get("spec", {})
            if original.get("action") not in {"rename", "parts", "next", "change", "progress", "wait", "arrival", "note"}:
                raise CardError("Check this retained answer in a fresh form before saving it.")
            spec = {**original, "ref": spec["ref"], "expected": spec["expected"], "line": None, "label": "Apply kept answer"}
            fields = draft
            intent["consumed_draft"] = draft.get("submission_id")
            kind = spec["action"]
        def resolve(value, error="Name one exact requirement title or ID. Nothing changed."):
            matches = [ref for ref, detail in view["details"].items() if value in {ref, detail["title"]}]
            if len(matches) != 1:
                raise CardError(error)
            return matches[0]
        if kind == "under":
            parent_ref = resolve(words(fields, limit=256), "Choose one exact existing project title or ID. Nothing changed.")
            parent = view["details"][parent_ref]
            if parent_ref == spec.get("ref") or parent["state"] in {"done", "dropped", "no_longer_needed"} or view.get("index") is not None and self._parent_kind(parent_ref, view) != "project":
                raise CardError("Choose an open project. Nothing changed.")
            spec = dict(spec, parent_ref=parent_ref, parent_title=parent["title"], parent_expected={"record_id": parent["record_id"], "sha256": parent["record_sha256"]})
        if kind == "requirement":
            spec["requirements"] = {"remove": [resolve(fields.get("remove", ""))], "add": [resolve(fields["add"])] if fields.get("add", "").strip() else []}
        if kind.startswith("planning_"):
            spec = dict(spec)
            if kind == "planning_shared":
                refs = []
                for value in (fields.get("tasks", []) if isinstance(fields.get("tasks"), list) else fields.get("tasks", "").split(",")):
                    refs.append(resolve(value.strip(), "Choose exact task titles or IDs for the shared block. Nothing changed."))
                spec["task_refs"] = list(dict.fromkeys(refs))
            if kind == "planning_operation":
                value = fields.get("followup", "")
                spec["followup_ref"] = resolve(value, "Choose one exact follow-up title or ID; nothing changed.")
        request = make_request(operation, spec, fields, date.fromisoformat(state["day"]))
        intent["request"] = request_data(request)
        if kind == "drop":
            after["last_drop"] = dict(change=operation, ref=spec["ref"], title=view["details"][spec["ref"]]["title"], scope=spec.get("scope") or "task")
        elif kind == "remove_plan":
            after.update(screen="day_summary", stage="day", item=None, page=0)
            after.pop("day_approval", None)
        elif kind == "undo_drop":
            after.pop("last_drop", None)
        after["win"] = spec["label"] + (" \u00b7 " + view["item"]["title"] if view.get("item") else "")
        if kind == "add":
            if state["screen"] != "anything":
                after["return_to"] = location(state)
            after.update(screen="inbox" if "stages" in view else "new", stage="inbox" if "stages" in view else "new", item=None, win="Your new items are saved")
        elif kind == 'arrival':
            after.update(screen='arrival_saved', arrival_context=wait_context(view['details'][spec['ref']], view.get('index', {})),
                         arrival_title=view['details'][spec['ref']]['title'], item=None)
        elif kind == "date" and view["item"]["length_minutes"] == 0:
            after.update(screen="item_length", length_ref=spec["ref"], not_yet=False)
        elif kind in {"length", "length_custom"} and state.get("settling"):
            # The final length leads to a fresh yes screen; a dedicated token
            # can settle only the exact, freshly read selected set.
            after.update(screen="day", item=None)
        elif kind == "remove_plan":
            pass
        elif kind in {"save", "rename"}:
            after.update(screen=state.get("stage", "new"), not_yet=False)
        else:
            after.update(screen=state.get("stage", "new"), item=None, not_yet=False)
        return intent

    async def _execute(self, intent):
        if intent["action"] in {"ask_send", "ask_words"}:
            value = await self.diablo.send(intent["payload"], owner_id=intent["owner_id"])
            return {**value, "status": "accepted", "remote_observed": True}
        result = {"status": "accepted", "remote_observed": True}
        if intent.get("request"):
            result = await self.backend.change(restore_request(intent["request"]), owner_id=intent["owner_id"])
        if intent["action"] == "confirm" and result.get("status") in {"accepted", "duplicate"} and result.get("remote_observed") is True:
            await self.backend.settle(intent["picks"], intent["eligible"], operation_id=intent["operation"],
                                      now=datetime.fromisoformat(intent["at"]), day=date.fromisoformat(intent["day"]))
        return result
