from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from gremlin_town.review_card.client import DiscordUnavailable
from gremlin_town.review_card.delivery import DiscordMorningSender, FALLBACK
from gremlin_town.review_card.locking import exclusive


@pytest.fixture
def anyio_backend():
    return 'asyncio'


class Store:
    def __init__(self, initial=None):
        self.value = initial or {}
    def delivery(self, day):
        return dict(self.value)
    def save_delivery(self, day, **values):
        self.value.update(values)
    def active_message_id(self):
        return self.value.get('message_id')


class Client:
    def __init__(self, store):
        self.store, self.posts, self.found, self.fail = store, 0, None, False
    def bot_user(self, application):
        return '102'
    def post(self, channel, message, *, nonce):
        assert self.store.value['attempted'] and self.store.value['nonce'] == nonce
        self.posts += 1
        if self.fail:
            raise DiscordUnavailable(uncertain=True)
        return {'id':'108'}
    def find_nonce(self, *args, **kwargs):
        return self.found


def setup(tmp_path, monkeypatch, initial=None):
    store = Store(initial)
    client = Client(store)
    class Engine:
        async def open(self, day):
            return SimpleNamespace(message={'components':[]})
        def bind_message(self, day, message_id):
            store.save_delivery(day, message_id=message_id)
    settings = SimpleNamespace(application_id='102', morning_channel_id='104')
    runs, requests = [], []
    context = SimpleNamespace(why_log=SimpleNamespace(record_run=lambda **values:runs.append(values)),service=object())
    packet = SimpleNamespace(prepared_at=datetime(2026,10,2,0,0,tzinfo=timezone.utc),bootstrap_input_sha=None,
                             request={'client_operation_id':'review-fictional'},short='Fictional review')
    def deliver(*args, **kwargs):
        requests.append(kwargs)
        return SimpleNamespace(error=None,request_id='fictional-request')
    monkeypatch.setattr('gremlin_town.review_card.delivery.deliver',deliver)
    return DiscordMorningSender(Engine(),client,settings,store), context, packet, runs, requests


@pytest.mark.anyio
async def test_confirmed_post_is_bound_before_morning_marked_sent(tmp_path, monkeypatch):
    sender, context, packet, runs, requests = setup(tmp_path,monkeypatch)
    receipt = await sender._send(context,packet,date(2026,10,2),str(tmp_path))
    assert receipt.request_id is None and not requests
    assert sender.store.value['message_id']=='108' and sender.store.value['phase']=='sent'
    assert len(runs)==1
    await sender._send(context,packet,date(2026,10,2),str(tmp_path))
    assert sender.client.posts==1


@pytest.mark.anyio
async def test_crash_after_post_reconciles_without_second_post(tmp_path, monkeypatch):
    sender,context,packet,runs,requests=setup(tmp_path,monkeypatch,{'attempted':True,'nonce':'original'})
    sender.client.found={'id':'108'}
    await sender._send(context,packet,date(2026,10,2),str(tmp_path))
    assert sender.client.posts==0 and not requests and len(runs)==1


@pytest.mark.anyio
async def test_uncertain_delivery_falls_back_with_reason_and_never_reposts(tmp_path,monkeypatch):
    sender,context,packet,runs,requests=setup(tmp_path,monkeypatch)
    sender.client.fail=True
    await sender._send(context,packet,date(2026,10,2),str(tmp_path))
    assert sender.store.value['route']=='telegram'
    assert (tmp_path/'review-fictional.txt').read_text()==FALLBACK+'\n\nFictional review'
    sender.client.fail=False
    await sender._send(context,packet,date(2026,10,2),str(tmp_path))
    assert sender.client.posts==1 and requests[0]==requests[1]


def test_another_process_lock_refuses_delivery_until_holder_exits(tmp_path):
    path=tmp_path/'delivery.lock'
    with exclusive(path):
        with pytest.raises(RuntimeError,match='already running'):
            with exclusive(path):
                pytest.fail('overlapping delivery')
    with exclusive(path):
        pass
