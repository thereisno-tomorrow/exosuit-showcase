"""Authenticate Discord events and acknowledge before admitting any owner save."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from functools import partial
import inspect
import logging
import time

from .client import DiscordUnavailable
from .models import CardRenderError
from .latency import start_tap, finish_tap, phase, elapsed

logger = logging.getLogger(__name__)


async def _value(value):
    return await value if inspect.isawaitable(value) else value


def modal_fields(data):
    """Only submitted text/select values; never take routing from the browser."""
    output = {}
    def visit(components):
        if not isinstance(components, list) or len(components) > 40:
            raise ValueError("The submitted form could not be read")
        for component in components:
            if not isinstance(component, dict):
                raise ValueError("The submitted form could not be read")
            if "custom_id" in component and ("value" in component or "values" in component):
                key = component["custom_id"]
                value = component.get("value", component.get("values"))
                if not isinstance(key, str) or len(key) > 100 or key in output:
                    raise ValueError("The submitted form could not be read")
                if not ((isinstance(value, str) and len(value) <= 4000)
                        or (isinstance(value, list) and len(value) <= 25
                            and all(isinstance(entry, str) and len(entry) <= 100 for entry in value))):
                    raise ValueError("The submitted form is too long")
                if key not in {"tasks", "ids", "values"} and isinstance(value, list):
                    if len(value) != 1:
                        raise ValueError("Choose one option in each list")
                    value = value[0]
                output[key] = value
            if "components" in component:
                visit(component["components"])
            if "component" in component:
                visit([component["component"]])
    visit(data.get("components", []))
    return output


class InteractionHost:
    def __init__(self, engine, client, settings, *, clock=time.monotonic, threads=None):
        self.engine, self.client, self.settings = engine, client, settings
        self.clock = clock
        self.threads = threads
        self._thread_notice = ""
        # Saves, provider reads and model requests cannot consume ACK workers.
        self._acks = ThreadPoolExecutor(max_workers=2, thread_name_prefix="discord-ack")
        self._actions = asyncio.Lock()

    def close(self):
        self._acks.shutdown(wait=True)

    async def edit_card(self, result):
        from .render import payload, walk
        message = await _value(self.engine.active_message_id())
        if not message:
            return
        body = deepcopy(result.message)
        if self._thread_notice:
            texts = [item for item in walk(body.get("components", [])) if item.get("type") == 10]
            if not texts:
                raise ValueError("The card has no place for its delivery notice")
            first = texts[0]
            budget = 4000 - sum(len(item.get("content", "")) for item in texts[1:])
            notice = self._thread_notice if len(self._thread_notice) <= budget else "Thread unavailable."
            first["content"] = (notice + "\n" + first["content"])[:budget]
        body.update(payload(body.get("components", [])))
        with phase("edit_card"):
            await asyncio.to_thread(self.client.edit, self.settings.morning_channel_id, message, body)

    async def thread_status(self, result):
        if not isinstance(result, dict):
            raise RuntimeError("The thread bridge returned an invalid result")
        failed = result.get("outcome") in {"unavailable", "refused"}
        text = result.get("text", "The review thread could not be updated.") if failed else ""
        if text != self._thread_notice:
            self._thread_notice = text
            await self.edit_card(await self.engine.current())

    async def thread_message(self, event):
        if self.threads is None:
            return
        # The same lock spans draft preparation, durable intent and publication.
        # An owner refinement cannot replace a draft during its approved save.
        async with self._actions:
            result = await self.threads.on_message(event)
            if result.get("outcome") not in {"ignored", "disabled"}:
                await self.thread_status(result)
                if result.get("outcome") == "requested":
                    await self.edit_card(await self.engine.current())

    async def _ack(self, interaction, payload, received_at):
        remaining = 2.5 - (self.clock() - received_at)
        if remaining <= 0:
            raise DiscordUnavailable("The tap arrived too late; nothing was saved")
        try:
            with phase("acknowledge"):
                await asyncio.wait_for(asyncio.get_running_loop().run_in_executor(
                    self._acks, partial(self.client.callback, interaction, payload)), timeout=remaining)
        except TimeoutError:
            raise DiscordUnavailable("The tap was not acknowledged in time; nothing was saved") from None
        if self.clock() - received_at >= 2.5:
            raise DiscordUnavailable("The tap was not acknowledged in time; nothing was saved")

    async def interaction(self, event, *, received_at=None):
        token = start_tap()
        received_at = self.clock() if received_at is None else received_at
        try:
            from brain_core.github_storage import observe_github_requests
            from .latency import provider_elapsed
            with phase("total"), observe_github_requests(provider_elapsed):
                return await self._interaction(event, received_at=received_at)
        finally:
            elapsed("received_to_complete", self.clock() - received_at)
            finish_tap(token)

    async def _interaction(self, event, *, received_at=None):
        settings = self.settings
        if not (settings.enabled and settings.card):
            return False
        if not isinstance(event, dict) or event.get("type") not in (3, 5):
            return False
        received_at = self.clock() if received_at is None else received_at
        user = event.get("member", {}).get("user") or event.get("user") or {}
        thread_control = bool(self.threads is not None and self.threads.owns_interaction(event))
        authorized = (event.get("application_id") == settings.application_id
                      and event.get("guild_id") == settings.guild_id
                      and (event.get("channel_id") == settings.morning_channel_id or thread_control)
                      and user.get("id") == settings.owner_user_id and not user.get("bot"))
        if not authorized:
            # No private item data or auth distinction in the refusal.
            await self._ack(event, {"type": 4, "data": {"content": "This review isn't available here.",
                "flags": 64, "allowed_mentions": {"parse": []}}}, received_at)
            return False
        active_message = await _value(self.engine.active_message_id())
        source_message = event.get("message", {}).get("id")
        action_token = event.get("data", {}).get("custom_id")
        continuation = bool(event["type"] == 5 and isinstance(action_token, str)
            and hasattr(self.engine, "can_continue") and self.engine.can_continue(action_token, submitted=True))
        if source_message is not None and source_message != active_message and not thread_control and not continuation:
            await self._ack(event, {"type": 4, "data": {"content": "Open today's morning review.",
                "flags": 64, "allowed_mentions": {"parse": []}}}, received_at)
            return False
        if not active_message or (event["type"] == 3 and source_message is None):
            return False
        data = event.get("data", {})
        action = data.get("custom_id")
        if not isinstance(action, str) or not 1 <= len(action) <= 100:
            return False
        modal = None
        if event["type"] == 3:
            # This method only validates the persisted control and prepares a
            # form; it must not read providers, publish records or call Diablo.
            remaining = 2.0 - (self.clock() - received_at)
            if remaining <= 0:
                raise DiscordUnavailable("The tap arrived too late; nothing was saved")
            try:
                modal = await asyncio.wait_for(_value(self.engine.prepare_modal(action,
                    owner_user_id=settings.owner_user_id)), timeout=remaining)
            except TimeoutError:
                raise DiscordUnavailable("The tap arrived too late; nothing was saved") from None
            except ValueError:
                await self._ack(event, {"type": 4, "data": {"content": "Open the latest review buttons and try again.",
                    "flags": 64, "allowed_mentions": {"parse": []}}}, received_at)
                return False
        await self._ack(event, modal if modal is not None else {"type": 6}, received_at)
        if modal is not None:
            return True
        try:
            fields = modal_fields(data) if event["type"] == 5 else {"values": data["values"]} if "values" in data else None
        except ValueError:
            await asyncio.to_thread(self.client.followup, event, settings.application_id,
                                    "The answer could not be read. Open the form and try again.")
            return False
        queued_at = self.clock()
        async with self._actions:
            elapsed("queue", self.clock() - queued_at)
            # A new day's card may have replaced this one while another save
            # finished. Never apply an old-card control to the new card.
            if await _value(self.engine.active_message_id()) != active_message:
                if not (event["type"] == 5 and hasattr(self.engine, "can_continue") and self.engine.can_continue(action, submitted=True)):
                    return False
                active_message = await _value(self.engine.active_message_id())
            async def progress(stage='received'):
                method = getattr(self.engine, 'action_progress', None)
                if method is None:
                    return
                try:
                    receipt = await _value(method(action, owner_user_id=settings.owner_user_id, stage=stage))
                    if receipt is not None:
                        await self.edit_card(receipt)
                except DiscordUnavailable:
                    logger.warning("Discord progress view unavailable; checked action continues")
            try:
                await progress()
                kwargs = {'owner_user_id': settings.owner_user_id}
                if 'progress' in inspect.signature(self.engine.handle).parameters:
                    kwargs['progress'] = progress
                result = await self.engine.handle(action, event["id"], fields, **kwargs)
            except CardRenderError:
                await asyncio.to_thread(self.client.followup, event, settings.application_id,
                                        "The review view could not be displayed. Your save result is unchanged; open the latest review.")
                return False
            except ValueError:
                await asyncio.to_thread(self.client.followup, event, settings.application_id,
                                        "That action could not be checked. Use the latest review controls; submitted answers remain kept.")
                return False
            if await _value(self.engine.active_message_id()) == active_message:
                await self.edit_card(result)
                # Duration only: never log the interaction, token, owner or item.
                logger.info("Discord card updated in %.3f seconds", self.clock() - received_at)
            if self.threads is not None:
                await self.thread_status(await self.threads.sync())
        return True
