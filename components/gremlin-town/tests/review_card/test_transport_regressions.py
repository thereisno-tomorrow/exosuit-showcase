"""Independent transport regressions, entirely fake transport and invented data."""
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
import websocket

from gremlin_town.review_card.client import DiscordClient, DiscordUnavailable
from gremlin_town.review_card.gateway import Gateway
from tests.review_card.test_gateway import Socket, event, ready


def test_complete_ack_is_bounded_even_when_transport_ignores_phase_timeout(monkeypatch):
    import gremlin_town.review_card.client as client_module
    monkeypatch.setattr(client_module, '_ACK_SECONDS', 0.05)
    release = threading.Event()
    entered = threading.Event()
    def handler(request):
        entered.set()
        assert release.wait(1)
        return httpx.Response(204)
    api = DiscordClient('fictional-token', http=httpx.Client(transport=httpx.MockTransport(handler)))
    started = time.monotonic()
    try:
        with pytest.raises(DiscordUnavailable, match='acknowledge'):
            api.callback({'id': '201', 'token': 'fictional-interaction'}, {'type': 6})
        assert entered.is_set() and time.monotonic() - started < 0.5
    finally:
        release.set()
        api.close()


def test_late_transport_success_is_not_an_acknowledgement():
    now = [0.0]
    def handler(request):
        now[0] += 9.0
        return httpx.Response(204)
    api = DiscordClient('fictional-token', http=httpx.Client(transport=httpx.MockTransport(handler)),
                        clock=lambda: now[0])
    try:
        with pytest.raises(DiscordUnavailable, match='acknowledge'):
            api.callback({'id': '201', 'token': 'fictional-interaction'}, {'type': 6})
    finally:
        api.close()


def test_failed_handshake_sends_no_gateway_credentials():
    sock = Socket([], [0.0])
    sock.getstatus = lambda: 302
    gateway = Gateway(SimpleNamespace(_token='fictional'), lambda *args: None,
                      application_id='102', socket_factory=lambda *a, **kw: sock)
    with pytest.raises(DiscordUnavailable, match='handshake'):
        gateway._connection('wss://gateway.discord.gg')
    assert not sock.sent


def test_reply_to_server_close_preserves_session(monkeypatch):
    from gremlin_town.review_card.gateway import _ResumeSocket
    sent = []
    monkeypatch.setattr(websocket.WebSocket, 'send_close', lambda self, status, reason: sent.append(status))
    _ResumeSocket().send_close()
    assert sent == [4000]


def test_reconnect_preserves_the_resume_session():
    now = [0.0]
    closes = []

    class Stop:
        done = False
        def is_set(self):
            return self.done
        def wait(self, _):
            self.done = True

    class RecordingSocket(Socket):
        def close(self, status=1000, **kwargs):
            closes.append(status)
            super().close()

    sock = RecordingSocket([event(10, {'heartbeat_interval': 45000}), ready(), event(7, None)], now)
    api = SimpleNamespace(_token='fictional-token', gateway=lambda: {'url': 'wss://gateway.discord.gg'})
    gateway = Gateway(api, lambda *args: None, application_id='102', stop=Stop(),
                      clock=lambda: now[0], socket_factory=lambda *a, **kw: sock, jitter=lambda: 0)
    gateway.run()
    assert closes and all(code not in (1000, 1001) for code in closes), 'resume session invalidated by close code: ' + repr(closes)


def test_gateway_redirect_never_receives_identify(monkeypatch):
    import websocket._core as core
    addresses = []
    sent_to = []

    class Wire:
        def __init__(self, url):
            self.url = url
        def close(self):
            pass
        def settimeout(self, _):
            pass

    def connect(url, *args, **kwargs):
        addresses.append(url)
        return Wire(url), ('fictional-host', 443, '/')

    def handshake(sock, url, *args, **kwargs):
        if len(addresses) == 1:
            return SimpleNamespace(status=302, headers={'location': 'wss://outside.example.test'}, subprotocol=None)
        return SimpleNamespace(status=101, headers={}, subprotocol=None)

    frames = iter([event(10, {'heartbeat_interval': 45000}), event(7, None)])
    monkeypatch.setattr(core, 'connect', connect)
    monkeypatch.setattr(core, 'handshake', handshake)
    monkeypatch.setattr(websocket.WebSocket, 'recv_data', lambda *a, **kw: next(frames))
    monkeypatch.setattr(websocket.WebSocket, 'send', lambda self, value: sent_to.append((self.sock.url, json.loads(value)['op'])))
    api = SimpleNamespace(_token='fictional-token')
    gateway = Gateway(api, lambda *args: None, application_id='102', jitter=lambda: 1)
    try:
        gateway._connection('wss://gateway.discord.gg')
    except (DiscordUnavailable, websocket.WebSocketException):
        pass
    assert not any('outside.example.test' in url for url, op in sent_to if op in (2, 6)), 'Identify sent to redirected non-Discord host'


def test_malformed_message_read_never_creates_thread():
    methods = []

    def handle(request):
        methods.append(request.method)
        return httpx.Response(200, json={} if request.method == 'GET' else {'id': '107'})

    api = DiscordClient('fictional-token', http=httpx.Client(transport=httpx.MockTransport(handle)))
    try:
        api.thread('104', '108')
    except (DiscordUnavailable, ValueError):
        pass
    finally:
        api.close()
    assert methods == ['GET'], 'created a thread after an unexpected message reply: ' + repr(methods)
