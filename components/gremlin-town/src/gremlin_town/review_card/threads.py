"""One daily review thread. GT retains draft text; this store retains bindings only."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import inspect
import json

from starlette.concurrency import run_in_threadpool

from brain_core.timewords import SINGAPORE
from gremlin_town.review_card.client import DiscordUnavailable, identifier
from gremlin_town.review_card.diablo import DiabloRefused


_FIELD = "diablo_threads"
_FAILURE = "The review thread could not be updated. Your saved work is unchanged."
_UNCERTAIN = "The draft's delivery needs checking. It has been kept without posting it twice."
_BINDING_FIELDS = ("day", "line_ref", "item_ref")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode()).hexdigest()


def _binding(value, day):
    if (not isinstance(value, dict) or value.get("day") != day
            or any(not isinstance(value.get(key), str) or not value[key] for key in (*_BINDING_FIELDS, "operation_id"))):
        raise ValueError("The review draft binding is unavailable")
    return {key: value[key] for key in (*_BINDING_FIELDS, "operation_id")}


def _parts(draft):
    """Plain, bounded Discord messages; never alter the canonical GT wording."""
    if draft["state"] == "nudge":
        text = draft.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("The nudge is unavailable")
        lines = ["Diablo's draft nudge", text]
    else:
        lines = ["Diablo's steps"]
        for number, step in enumerate(draft["steps"], 1):
            lines.append(f"{number}. {step['title']}\n{step['detail']}")
    # Splitting is display-only. The Save path re-reads the exact accepted GT reply.
    chunks, current = [], ""
    for line in lines:
        while line:
            space = 1800 - len(current) - (2 if current else 0)
            if space <= 0:
                chunks.append(current)
                current = ""
                continue
            piece, line = line[:space], line[space:]
            current += ("\n\n" if current else "") + piece
            if line:
                chunks.append(current)
                current = ""
    if current:
        chunks.append(current)
    return chunks


def _retire_rejected(state, binding_key, draft=None):
    """Only the currently approved draft can retry a definite delivery rejection."""
    for post in state["posts"].values():
        if (post["binding"] == binding_key and post["status"] == "rejected"
                and not (draft and draft.get("state") in {"draft", "nudge"}
                         and post["operation_id"] == draft.get("operation_id")
                         and post["draft_sha256"] == draft.get("draft_sha256"))):
            post["status"] = "superseded"


class ThreadBridge:
    """A single gateway owns this adapter and serializes its thread operations.

    The engine owns fresh Brain authorization and all writes. This bridge never
    saves a draft itself, and never stores draft or owner-message content.
    """

    def __init__(self, client, store, engine, diablo, settings, *, bot_user_id, now=None):
        self.client, self.store, self.engine, self.diablo = client, store, engine, diablo
        self.settings, self.bot_user_id = settings, identifier(bot_user_id)
        self.now = now or (lambda: datetime.now(SINGAPORE))
        self._lock = asyncio.Lock()

    def _enabled(self):
        return bool(self.settings.enabled and self.settings.card and self.settings.ask_diablo)

    def _day(self):
        return (self.now().astimezone(SINGAPORE) - timedelta(hours=6)).date().isoformat()

    def _load(self, day):
        delivery = self.store.delivery(day) or {}
        state = deepcopy(delivery.get(_FIELD) or {"bindings": {}, "posts": {}, "replies": {}, "revision": None})
        return delivery, state

    def _save(self, day, state):
        self.store.save_delivery(day, **{_FIELD: deepcopy(state)})

    async def _current(self):
        value = self.engine.current_diablo_binding()
        return await value if inspect.isawaitable(value) else value

    async def sync(self, day=None):
        """Poll local GT first; fresh Brain reads happen only for changed work."""
        if not self._enabled():
            return {"outcome": "disabled"}
        async with self._lock:
            try:
                return await self._sync(day or self._day())
            except Exception:
                # No provider exception, record, event content or identifier is
                # suitable for the card's error line or an application log.
                return {"outcome": "unavailable", "text": _FAILURE}

    async def _sync(self, day):
        if day != self._day():
            return {"outcome": "expired"}
        delivery, state = self._load(day)
        current = await self._current()
        changed = False
        if current is not None and current.get("day") == day:
            current = _binding(current, day)
            key = _digest({name: current[name] for name in _BINDING_FIELDS})
            previous = state["bindings"].get(key)
            if previous is None or previous.get("card_operation_id") != current["operation_id"]:
                state["bindings"][key] = {**current, "card_operation_id": current["operation_id"], "state": "waiting"}
                self._save(day, state)
                changed = True
        if not state["bindings"]:
            return {"outcome": "idle"}
        revision = await self.diablo.revision(day)
        unresolved = any(post["status"] in {"posting", "rejected"} for post in state["posts"].values())
        if not changed and not unresolved and revision == state.get("revision"):
            return {"outcome": "unchanged"}
        channel = delivery.get("channel_id") or self.settings.morning_channel_id
        message = delivery.get("message_id")
        if channel != self.settings.morning_channel_id or not message:
            return {"outcome": "waiting_for_card"}
        if not state.get("thread_id"):
            # thread() verifies the parent message and recovers its one existing
            # thread before attempting creation. Discord permits only one.
            thread = await run_in_threadpool(self.client.thread, channel, message)
            state["thread_id"] = identifier(thread)
            self._save(day, state)
        thread = state["thread_id"]
        uncertain = False
        for post in state["posts"].values():
            if post["status"] != "posting":
                continue
            found = await run_in_threadpool(self.client.find_nonce, thread, post["nonce"], bot_user_id=self.bot_user_id)
            if found is None:
                uncertain = True
                continue
            if (found.get("nonce") != post["nonce"] or found.get("author", {}).get("id") != self.bot_user_id
                    or found.get("channel_id") != thread):
                raise ValueError("The draft delivery could not be verified")
            post.update(status="posted", message_id=identifier(found.get("id")))
            self._save(day, state)
        if uncertain:
            return {"outcome": "unavailable", "text": _UNCERTAIN}
        sent = 0
        for key, binding in state["bindings"].items():
            base = {name: binding[name] for name in _BINDING_FIELDS}
            try:
                draft = await self.diablo.read(base, owner_id=self.settings.owner_id)
            except ValueError:
                # The engine's safe CardError and DiabloRefused both inherit
                # ValueError. A closed older line must not block other drafts.
                binding["state"] = "unavailable"
                _retire_rejected(state, key)
                self._save(day, state)
                continue
            _retire_rejected(state, key, draft)
            binding["state"] = draft["state"]
            if draft.get("operation_id"):
                binding["operation_id"] = draft["operation_id"]
            if draft["state"] not in {"draft", "nudge"}:
                self._save(day, state)
                continue
            digest = draft["draft_sha256"]
            chunks = _parts(draft)
            for number, content in enumerate(chunks):
                post_key = _digest([key, draft["operation_id"], digest, number])
                post = state["posts"].get(post_key)
                if post is not None and post["status"] in {"posted", "posting"}:
                    continue
                if post is None:
                    components = []
                    if number == len(chunks) - 1 and draft["state"] == "draft":
                        button = self.engine.draft_control({**base, "operation_id": draft["operation_id"]})
                        button = await button if inspect.isawaitable(button) else button
                        components = [{"type": 1, "components": [button]}]
                    post = {"binding": key, "operation_id": draft["operation_id"], "draft_sha256": digest,
                            "part": number, "nonce": "rd" + post_key[:23], "components": components,
                            "status": "posting"}
                    state["posts"][post_key] = post
                else:
                    post["status"] = "posting"
                self._save(day, state)  # Intent reaches durable storage before REST.
                try:
                    result = await run_in_threadpool(self.client.post, thread,
                        {"content": content, "components": post["components"]}, nonce=post["nonce"])
                    post.update(status="posted", message_id=identifier(result.get("id")))
                    self._save(day, state)
                    sent += 1
                except DiscordUnavailable as exc:
                    if not exc.uncertain:
                        post["status"] = "rejected"
                        self._save(day, state)
                    return {"outcome": "unavailable", "text": _UNCERTAIN if exc.uncertain else _FAILURE}
        state["revision"] = revision
        self._save(day, state)
        return {"outcome": "updated", "sent": sent}

    async def on_message(self, event):
        """Refine only exact owner messages in this review's persisted thread."""
        if not self._enabled():
            return {"outcome": "disabled"}
        async with self._lock:
            try:
                return await self._message(event)
            except DiabloRefused as exc:
                return {"outcome": "refused", "text": str(exc)}
            except Exception:
                return {"outcome": "unavailable", "text": _FAILURE}

    async def _message(self, event):
        day = self._day()
        _, state = self._load(day)
        if (not isinstance(event, dict) or event.get("guild_id") != self.settings.guild_id
                or event.get("channel_id") != state.get("thread_id") or not state.get("thread_id")
                or event.get("author", {}).get("id") != self.settings.owner_user_id
                or event.get("author", {}).get("bot")):
            return {"outcome": "ignored"}
        event_id = identifier(event.get("id"))
        text = event.get("content")
        if not isinstance(text, str) or not text.strip() or len(text) > 4000 or "\x00" in text:
            raise DiabloRefused("Use one message of at most 4,000 characters to refine the draft.")
        frozen = state["replies"].get(event_id)
        reference = event.get("message_reference")
        if frozen is not None:
            if frozen["words_sha256"] != hashlib.sha256(text.encode()).hexdigest():
                raise DiabloRefused("This reply has changed. Send your new words as a new message.")
            matches = {frozen["binding"]}
            previous_operation = frozen["previous_operation_id"]
        elif reference is not None:
            if (not isinstance(reference, dict) or reference.get("channel_id", state["thread_id"]) != state["thread_id"]
                    or reference.get("guild_id", self.settings.guild_id) != self.settings.guild_id):
                raise DiabloRefused("Reply to the draft you want Diablo to change.")
            matches = {post["binding"] for post in state["posts"].values()
                       if post.get("message_id") == reference.get("message_id") and post["status"] == "posted"}
            previous_operation = next((post["operation_id"] for post in state["posts"].values()
                if post.get("message_id") == reference.get("message_id") and post["status"] == "posted"), None)
        else:
            # A pending request is also active. Never choose the one ready draft
            # when the owner's unthreaded words might concern another request.
            matches = {key for key, binding in state["bindings"].items()
                       if binding.get("state") not in {"none", "stale", "unavailable"}}
            previous_operation = None
        if len(matches) != 1:
            raise DiabloRefused("Reply to the draft you want Diablo to change.")
        key = matches.pop()
        binding = state["bindings"][key]
        try:
            current = self.engine.authorize_item(self.settings.owner_id, day, binding["line_ref"], binding["item_ref"])
            current = await current if inspect.isawaitable(current) else current
        except ValueError:
            raise DiabloRefused("This review item changed. Open its current card before replying.") from None
        if not isinstance(current, dict) or current.get("ref") != binding["item_ref"]:
            raise DiabloRefused("This review item changed. Open its current card before replying.")
        if frozen is None:
            frozen = {"binding": key, "previous_operation_id": previous_operation or binding["operation_id"],
                      "expected": {"record_id": current["record_id"], "sha256": current["record_sha256"]},
                      "words_sha256": hashlib.sha256(text.encode()).hexdigest()}
            state["replies"][event_id] = frozen
            self._save(day, state)
        value = {name: binding[name] for name in _BINDING_FIELDS}
        value.update(operation_id="review-reply-" + event_id, previous_operation_id=frozen["previous_operation_id"],
                     expected=frozen["expected"], text=text)
        result = await self.diablo.refine(value, owner_id=self.settings.owner_id)
        binding.update(operation_id=result["operation_id"], state=result["state"])
        state["revision"] = None
        self._save(day, state)
        return {"outcome": "requested", "text": "Diablo is refining that draft."}

    def owns_interaction(self, event):
        """Permit only stored thread controls; the engine still checks its token."""
        if not self._enabled() or not isinstance(event, dict):
            return False
        _, state = self._load(self._day())
        user = event.get("member", {}).get("user", {})
        if (event.get("guild_id") != self.settings.guild_id or user.get("id") != self.settings.owner_user_id
                or event.get("channel_id") != state.get("thread_id") or not state.get("thread_id")):
            return False
        message = event.get("message", {})
        return any(post.get("message_id") == message.get("id") and post["status"] == "posted"
                   and any(control.get("custom_id") == event.get("data", {}).get("custom_id")
                           for row in post.get("components", ()) for control in row["components"])
                   for post in state["posts"].values())
