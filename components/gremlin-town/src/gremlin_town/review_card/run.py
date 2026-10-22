"""Hub-only Discord listener; its outbound connection exposes no public port."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
from dataclasses import replace
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time
import tomllib

from .client import DiscordClient, DiscordUnavailable, read_credential
from .gateway import Gateway
from .host import InteractionHost
from .locking import exclusive
from .settings import DiscordSettings

logger = logging.getLogger(__name__)


def load_settings(path):
    with Path(path).open('rb') as source:
        settings = DiscordSettings.from_table(tomllib.load(source).get('discord'), base=Path(path).resolve().parent)
    secret = os.environ.get('GT_DISCORD_TOKEN_FILE')
    return replace(settings, token_file=Path(secret)) if secret else settings


async def serve(runtime, settings, client, *, stop=None, gateway_factory=Gateway):
    from .composition import build_engine
    from .store import CardStore
    from .threads import ThreadBridge
    from .models import review_day
    now = lambda: datetime.now(timezone.utc)
    stop = stop or threading.Event()
    store = CardStore(settings.state_file)
    host = None
    pending = set()
    failure = asyncio.get_running_loop().create_future()
    gateway_task = None
    try:
        engine = build_engine(runtime, settings, store, now)
        bot = await asyncio.to_thread(client.bot_user, settings.application_id)
        threads = ThreadBridge(client, store, engine, engine.diablo, settings, bot_user_id=bot, now=now) if settings.ask_diablo else None
        host = InteractionHost(engine, client, settings, threads=threads)
        loop = asyncio.get_running_loop()

        def finished(task):
            pending.discard(task)
            if task.cancelled():
                return
            error = task.exception()
            if error is not None and not failure.done():
                # Keep only a generic failure: original exceptions can contain
                # provider paths, callback tokens, or private record text.
                failure.set_result(True)

        async def event(name, data, received_at):
            if name == 'INTERACTION_CREATE':
                try:
                    await host.interaction(data, received_at=received_at)
                except DiscordUnavailable:
                    logger.warning('Discord could not complete a tap; its saved intent is retained')
            elif name == 'MESSAGE_CREATE' and threads is not None:
                await host.thread_message(data)

        def enqueue(name, data, received_at):
            if name not in {'INTERACTION_CREATE', 'MESSAGE_CREATE'}:
                return
            if len(pending) >= 16:
                if not failure.done():
                    failure.set_result(True)
                return
            task = asyncio.create_task(event(name, data, received_at))
            pending.add(task)
            task.add_done_callback(finished)

        gateway = gateway_factory(client,
            lambda name, data: loop.call_soon_threadsafe(enqueue, name, data, time.monotonic()),
            application_id=settings.application_id, read_threads=settings.ask_diablo, stop=stop)
        gateway_task = asyncio.create_task(asyncio.to_thread(gateway.run))
        while not stop.is_set():
            done, _ = await asyncio.wait({gateway_task, failure}, timeout=5, return_when=asyncio.FIRST_COMPLETED)
            if failure in done:
                raise RuntimeError('The Discord listener stopped after an unexpected event failure')
            if gateway_task in done:
                await gateway_task
                break
            if not gateway.ready.is_set():
                continue
            delivered = store.delivery(engine._day())
            if not engine.active_message_id():
                # The courier owns delivery. The existing active card can continue
                # across local midnight without creating another Discord message.
                continue
            async with host._actions:
                result = await engine.refresh_pending()
                if result is not None and (message := engine.active_message_id()):
                    await host.edit_card(result)
                if threads is not None:
                    await host.thread_status(await threads.sync())
    finally:
        stop.set()
        if gateway_task is not None:
            gateway.close()
            await asyncio.gather(gateway_task, return_exceptions=True)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if host is not None:
            host.close()
        store.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='gremlin-town discord')
    parser.add_argument('command', choices=('serve',))
    parser.add_argument('--config', required=True, type=Path)
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    runtime = client = None
    stop = threading.Event()
    try:
        settings = load_settings(args.config)
        if not (settings.enabled and settings.card):
            sys.stderr.write('Discord morning card is off. No connection was opened.\n')
            return 2
        from gremlin_town.runtime import build_runtime
        with exclusive(settings.state_file.with_suffix('.gateway.lock')):
            runtime = build_runtime(args.config, start_worker=False)
            client = DiscordClient(read_credential(settings.token_file))
            previous = {}
            for sig in (signal.SIGINT, signal.SIGTERM):
                previous[sig] = signal.signal(sig, lambda *_: stop.set())
            async def owned_loop():
                try:
                    await serve(runtime, settings, client, stop=stop)
                finally:
                    if runtime.brain_runtime is not None:
                        await runtime.brain_runtime.aclose()
            try:
                asyncio.run(owned_loop())
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
        return 0
    except Exception:
        sys.stderr.write('Discord listener stopped. Check its configuration and protected files before restarting.\n')
        return 2
    finally:
        if client is not None:
            client.close()
        if runtime is not None:
            runtime.close()
