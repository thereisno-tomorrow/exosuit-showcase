import asyncio
from types import SimpleNamespace

import pytest

from gremlin_town.review_card.client import DiscordUnavailable
from gremlin_town.review_card.host import InteractionHost, modal_fields
from gremlin_town.review_card.settings import DiscordSettings


@pytest.fixture
def anyio_backend():
    return 'asyncio'


def settings():
    return DiscordSettings(enabled=True, card=True, application_id='102', guild_id='101',
                           owner_user_id='103', owner_id='fictional-owner', morning_channel_id='104')


def event():
    return dict(type=3, id='201', token='fictional-callback', application_id='102', guild_id='101',
                channel_id='104', member={'user': {'id': '103'}}, message={'id': '108'},
                data={'custom_id': 'rc:fictional'})


class Engine:
    def __init__(self, calls):
        self.calls, self.active, self.form = calls, '108', None
    def active_message_id(self):
        return self.active
    async def prepare_modal(self, *args, **kwargs):
        return self.form
    async def handle(self, *args, **kwargs):
        self.calls.append('save')
        return SimpleNamespace(message={'components': []})


class Client:
    def __init__(self, calls):
        self.calls, self.fail = calls, False
    def callback(self, event, payload):
        self.calls.append(('ack', payload))
        if self.fail:
            raise DiscordUnavailable('Fictional failed acknowledgement')
    def edit(self, *args):
        self.calls.append('edit')


@pytest.mark.anyio
async def test_ack_happens_before_any_save_and_modal_does_not_save():
    calls = []
    engine, client = Engine(calls), Client(calls)
    host = InteractionHost(engine, client, settings())
    try:
        assert await host.interaction(event())
        assert [entry[0] if isinstance(entry, tuple) else entry for entry in calls] == ['ack', 'save', 'edit']
        calls.clear()
        engine.form = {'type': 9, 'data': {'custom_id': 'fictional-form'}}
        assert await host.interaction(event())
        assert calls == [('ack', engine.form)]
    finally:
        host.close()


@pytest.mark.anyio
async def test_failed_or_late_ack_never_reaches_save():
    calls = []
    client = Client(calls)
    client.fail = True
    host = InteractionHost(Engine(calls), client, settings())
    try:
        with pytest.raises(DiscordUnavailable):
            await host.interaction(event())
        assert 'save' not in calls
        calls.clear()
        with pytest.raises(DiscordUnavailable):
            await host.interaction(event(), received_at=host.clock() - 3)
        assert not calls
    finally:
        host.close()


@pytest.mark.anyio
@pytest.mark.parametrize('field,value', [('application_id','999'), ('guild_id','999'), ('channel_id','999'),
    ('member', {'user': {'id': '999'}}), ('message', {'id':'999'})])
async def test_foreign_or_replaced_card_can_never_reach_engine(field, value):
    calls = []
    host = InteractionHost(Engine(calls), Client(calls), settings())
    payload = event()
    payload[field] = value
    try:
        assert not await host.interaction(payload)
        assert 'save' not in calls
    finally:
        host.close()


def test_modal_parser_accepts_nested_labels_but_refuses_duplicate_fields():
    data = {'components': [{'type':18,'component':{'custom_id':'value','value':'Fictional owner words'}}]}
    assert modal_fields(data) == {'value':'Fictional owner words'}
    data['components'] *= 2
    with pytest.raises(ValueError):
        modal_fields(data)


def test_date_selects_are_single_values_and_multiple_choices_are_refused():
    data={'components':[{'type':18,'component':{'type':3,'custom_id':'week','values':['2026-10-05']}},
                        {'type':18,'component':{'type':21,'custom_id':'day','values':['0']}}]}
    assert modal_fields(data)=={'week':'2026-10-05','day':'0'}
    data['components'][1]['component']['values']=['0','1']
    with pytest.raises(ValueError,match='one option'):
        modal_fields(data)


@pytest.mark.anyio
async def test_thread_notice_fits_full_component_card_and_text_budget():
    from gremlin_town.review_card.render import render,walk
    from tests.review_card.test_render import fixture
    state,view=fixture()
    state.update(screen='day',win='Fixture saved',error='Fixture retry notice')
    message=render(state,view,lambda *args:'fixture')
    assert len(list(walk(message['components'])))<=40
    sent=[]
    host=InteractionHost(SimpleNamespace(active_message_id=lambda:'108'),
        SimpleNamespace(edit=lambda channel,mid,body:sent.append(body)),settings())
    try:
        host._thread_notice='The review thread could not be updated.'
        await host.edit_card(SimpleNamespace(message=message))
        assert len(list(walk(sent[0]['components'])))<=40
        assert sum(len(v.get('content','')) for v in walk(sent[0]['components']))<=4000
        assert sent[0]['components']!=message['components']
    finally:
        host.close()


@pytest.mark.anyio
async def test_elapsed_receipt_deadline_refuses_late_success():
    calls, now = [], [1.8]
    client = Client(calls)
    def callback(*args):
        now[0] += 0.9
    client.callback = callback
    host = InteractionHost(Engine(calls), client, settings(), clock=lambda:now[0])
    try:
        with pytest.raises(DiscordUnavailable, match='in time'):
            await host.interaction(event(), received_at=0)
        assert 'save' not in calls
    finally:
        host.close()


@pytest.mark.anyio
async def test_stale_local_control_is_refused_before_any_save():
    calls=[]
    engine=Engine(calls)
    async def refused(*args, **kwargs):
        raise ValueError('fictional-private-internal-data')
    engine.prepare_modal=refused
    host=InteractionHost(engine, Client(calls), settings())
    try:
        assert not await host.interaction(event())
        assert 'save' not in calls and 'fictional-private' not in str(calls)
    finally:
        host.close()


@pytest.mark.anyio
async def test_thread_refinement_waits_for_exact_owner_save_to_finish():
    calls=[]
    saving, finish=asyncio.Event(),asyncio.Event()
    engine=Engine(calls)
    async def save(*args,**kwargs):
        saving.set()
        await finish.wait()
        calls.append('saved')
        return SimpleNamespace(message={'components':[]})
    engine.handle=save
    class Threads:
        def owns_interaction(self,event):
            return False
        async def sync(self):
            return {'outcome':'unchanged'}
        async def on_message(self,event):
            calls.append('refine')
            return {'outcome':'ignored'}
    host=InteractionHost(engine,Client(calls),settings(),threads=Threads())
    try:
        save_task=asyncio.create_task(host.interaction(event()))
        await saving.wait()
        reply_task=asyncio.create_task(host.thread_message({'fictional':'reply'}))
        await asyncio.sleep(0)
        assert 'refine' not in calls
        finish.set()
        await asyncio.gather(save_task,reply_task)
        assert calls.index('saved') < calls.index('refine')
    finally:
        finish.set()
        host.close()
