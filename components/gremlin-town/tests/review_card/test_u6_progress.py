import asyncio
from types import SimpleNamespace
import pytest
from gremlin_town.review_card.host import InteractionHost
from .test_host import Engine, Client, settings, event

@pytest.fixture
def anyio_backend():
    return 'asyncio'

@pytest.mark.anyio
async def test_visible_receipt_precedes_slow_checked_action():
    calls = []
    gate = asyncio.Event()
    class SlowEngine(Engine):
        def action_progress(self, token, *, owner_user_id, stage='received'):
            return SimpleNamespace(message={'components':[{'type':10, 'content':'Received. Checking your answer before saving.'}]})
        async def handle(self, *args, **kwargs):
            calls.append('checking')
            await gate.wait()
            calls.append('confirmed')
            return SimpleNamespace(message={'components':[]})
    class VisibleClient(Client):
        def edit(self, channel, message, payload):
            calls.append(('view', payload))
    host = InteractionHost(SlowEngine(calls), VisibleClient(calls), settings())
    try:
        pending = asyncio.create_task(host.interaction(event()))
        for _ in range(100):
            if 'checking' in calls: break
            await asyncio.sleep(.001)
        assert any(isinstance(c,tuple) and c[0]=='view' and 'Received' in str(c[1]) for c in calls)
        assert 'confirmed' not in calls
        gate.set()
        assert await pending
        assert calls[0][0] == 'ack'
    finally:
        gate.set()
        host.close()


@pytest.mark.anyio
async def test_progress_display_failure_does_not_prevent_checked_save():
    from gremlin_town.review_card.client import DiscordUnavailable
    calls = []
    class ProgressEngine(Engine):
        def action_progress(self, *args, **kwargs):
            return SimpleNamespace(message={'components':[{'type':10, 'content':'Received. Checking.'}]})
    class FailedProgressClient(Client):
        def edit(self, channel, message, payload):
            if 'Received' in str(payload):
                calls.append('failed-progress-display')
                raise DiscordUnavailable('fictional presentation outage')
            calls.append('edit')
    host = InteractionHost(ProgressEngine(calls), FailedProgressClient(calls), settings())
    try:
        assert await host.interaction(event())
        assert calls.index('failed-progress-display') < calls.index('save')
        assert calls[-1] == 'edit'
    finally:
        host.close()


@pytest.mark.anyio
async def test_host_displays_saving_when_engine_reports_durable_intent():
    calls = []
    class DurableEngine(Engine):
        def action_progress(self, token, *, owner_user_id, stage='received'):
            return SimpleNamespace(message={'components':[{'type':10, 'content':stage}]})
        async def handle(self, *args, progress=None, **kwargs):
            calls.append('durable-intent')
            if progress:
                await progress('saving')
            calls.append('confirmed')
            return SimpleNamespace(message={'components':[]})
    class VisibleClient(Client):
        def edit(self, channel, message, payload):
            calls.append(('view', payload))
    host = InteractionHost(DurableEngine(calls), VisibleClient(calls), settings())
    try:
        assert await host.interaction(event())
        saving = next(i for i, c in enumerate(calls) if isinstance(c,tuple) and c[0]=='view' and 'saving' in str(c[1]))
        assert calls.index('durable-intent') < saving < calls.index('confirmed')
    finally:
        host.close()
