"""One durable morning post; an uncertain send is reconciled, never repeated."""
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from dataclasses import replace
from types import SimpleNamespace

import anyio

from brain_core.timewords import today_in_singapore
from gremlin_town.review.deliver import deliver
from gremlin_town.review.run import TelegramMorningSender
from gremlin_town.review.spool import write_message
from .client import DiscordUnavailable
from .locking import exclusive

FALLBACK = "Discord could not confirm today's review. Your review is here on Telegram."


class RuntimeMorningSender:
    """Allocate the Discord surface only when the existing 06:00 job sends."""
    def __init__(self, runtime):
        self.runtime = runtime

    def send(self, context, morning, references, *, inputs, now, spool):
        from .client import DiscordClient, read_credential
        from .composition import build_engine
        from .store import CardStore
        settings = self.runtime.discord
        if os.environ.get("GT_DISCORD_TOKEN_FILE"):
            settings = replace(settings, token_file=Path(os.environ["GT_DISCORD_TOKEN_FILE"]))
        store = CardStore(settings.state_file)
        client = None
        try:
            # A missing bot credential still uses the existing Telegram route.
            try:
                client = DiscordClient(read_credential(settings.token_file))
            except DiscordUnavailable:
                class Unavailable:
                    def bot_user(self, *args):
                        raise DiscordUnavailable()
                client = Unavailable()
            engine = build_engine(self.runtime, settings, store, lambda: now)
            sender = DiscordMorningSender(engine, client, settings, store,
                cleanup=self.runtime.brain_runtime.aclose if self.runtime.brain_runtime else None)
            return sender.send(context, morning, references, inputs=inputs, now=now, spool=spool)
        except Exception:
            raise RuntimeError("Morning card delivery failed; its durable intent is retained for inspection") from None
        finally:
            if client is not None and hasattr(client, "close"):
                client.close()
            store.close()


class DiscordMorningSender:
    def __init__(self, engine, client, settings, store, *, cleanup=None):
        self.engine, self.client, self.settings, self.store = engine, client, settings, store
        self.cleanup = cleanup

    def send(self, context, morning, references, *, inputs, now, spool):
        if not (self.settings.enabled and self.settings.card and self.settings.delivery):
            return TelegramMorningSender().send(context, morning, references, inputs=inputs, now=now, spool=spool)
        if spool is None:
            raise RuntimeError("Discord morning delivery requires the existing Telegram fallback spool")
        with exclusive(self.settings.state_file.with_suffix(".delivery.lock")):
            day = today_in_singapore(now)
            packet = context.why_log.morning_packet(day)
            if packet is None:
                if morning is None:
                    raise RuntimeError("The morning retry has no prepared packet")
                packet = TelegramMorningSender()._prepare(context, morning, references, inputs=inputs, now=now, spool=spool)
            return anyio.run(self._send_and_close, context, packet, day, spool)

    async def _send_and_close(self, *args):
        try:
            return await self._send(*args)
        finally:
            if self.cleanup is not None:
                await self.cleanup()

    def _completed(self, context, packet):
        context.why_log.record_run(kind="review", started_at=packet.prepared_at, last_event_cursor=None,
            outcome="sent", bootstrap_input_sha=packet.bootstrap_input_sha)
        return SimpleNamespace(request_id=None)

    async def _send(self, context, packet, day, spool):
        state = self.store.delivery(day)
        if state.get("route") == "telegram":
            return await asyncio.to_thread(self._fallback, context, packet, day, spool)
        if state.get("phase") == "sent" and state.get("message_id"):
            return self._completed(context, packet)
        nonce = state.get("nonce") or hashlib.sha256((self.settings.application_id + str(day)).encode()).hexdigest()[:24]
        channel = self.settings.morning_channel_id
        try:
            # A prior crash may have occurred after Discord accepted the post.
            if state.get("attempted"):
                bot = await asyncio.to_thread(self.client.bot_user, self.settings.application_id)
                message = await asyncio.to_thread(self.client.find_nonce, channel, nonce, bot_user_id=bot)
                if message is None:
                    raise DiscordUnavailable("Discord delivery could not be confirmed", uncertain=True)
            else:
                await asyncio.to_thread(self.client.bot_user, self.settings.application_id)
                old = self.store.active_message_id()
                if old is not None:
                    await asyncio.to_thread(self.client.edit, channel, old,
                        {"flags": 32768, "components": [{"type": 10, "content": "Replaced by today's review."}]})
                result = await self.engine.open(str(day))
                self.store.save_delivery(day, route="discord", attempted=True, phase="sending", nonce=nonce, channel_id=channel)
                message = await asyncio.to_thread(self.client.post, channel, result.message, nonce=nonce)
            self.engine.bind_message(day, message["id"])
            self.store.save_delivery(day, phase="sent")
            return self._completed(context, packet)
        except DiscordUnavailable:
            # Route choice is durable before creating the existing GT request.
            # Even a successful-but-unconfirmed Discord send cannot be repeated.
            self.store.save_delivery(day, route="telegram", phase="fallback")
            return await asyncio.to_thread(self._fallback, context, packet, day, spool)

    def _fallback(self, context, packet, day, spool):
        receipt = deliver(context.service, **packet.request)
        if receipt.error is not None:
            raise DiscordUnavailable("The Telegram fallback request could not be confirmed")
        write_message(spool, packet.request["client_operation_id"], FALLBACK + "\n\n" + packet.short)
        self._completed(context, packet)
        self.store.save_delivery(day, phase="fallback_sent")
        return receipt
