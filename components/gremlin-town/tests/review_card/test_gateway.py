import json
import struct
import threading
from types import SimpleNamespace

import pytest
import websocket

from gremlin_town.review_card.client import DiscordUnavailable
from gremlin_town.review_card.gateway import Gateway


def event(op, data, **extra):
    return (websocket.ABNF.OPCODE_TEXT, json.dumps({"op": op, "d": data, **extra}).encode())


def ready():
    return event(0, {"application": {"id": "102"}, "user": {"id": "103", "bot": True},
                     "session_id": "fictional-session", "resume_gateway_url": "wss://gateway.discord.gg"},
                 t="READY", s=7)


class Socket:
    def __init__(self, frames, now):
        self.frames = iter(frames)
        self.now = now
        self.sent = []
        self.closed = False

    def settimeout(self, value):
        assert value == 0.2

    def recv_data(self, *, control_frame):
        assert control_frame
        self.now[0] += 0.25
        try:
            return next(self.frames)
        except StopIteration:
            raise websocket.WebSocketTimeoutException()

    def send(self, value):
        self.sent.append(json.loads(value))

    def getstatus(self):
        return 101

    def close(self, **kwargs):
        self.closed = True


def build(frames, on_event=lambda *args: None):
    now = [0.0]
    sock = Socket(frames, now)
    connections = []
    def connect(url, **kwargs):
        connections.append((url, kwargs))
        return sock
    api = SimpleNamespace(_token="fictional-bot-secret", gateway=lambda: {"url": "wss://gateway.discord.gg"})
    gateway = Gateway(api, on_event, application_id="102", read_threads=True,
                      clock=lambda: now[0], socket_factory=connect, jitter=lambda: 0)
    return gateway, sock, now, connections


def test_identify_ready_events_heartbeat_and_resume():
    events = []
    gateway, sock, now, calls = build([event(10, {"heartbeat_interval": 1000}), ready(), event(11, None),
        event(0, {"fictional": True}, t="INTERACTION_CREATE", s=8), event(7, None)],
        lambda *args: events.append(args))
    gateway._connection("wss://gateway.discord.gg")
    assert sock.sent[0]["op"] == 2
    assert sock.sent[0]["d"]["intents"] == (1 << 9) | (1 << 15)
    assert any(row["op"] == 1 for row in sock.sent)
    assert [name for name, _ in events] == ["READY", "INTERACTION_CREATE"]
    assert gateway.sequence == 8 and gateway.session_id == "fictional-session"
    sock.frames = iter([event(10, {"heartbeat_interval": 1000}), event(7, None)])
    count = len(sock.sent)
    gateway._connection(gateway.resume_url)
    assert sock.sent[count]["op"] == 6
    assert sock.sent[count]["d"]["seq"] == 8


def test_missing_heartbeat_ack_reconnects_without_dispatching_fake_success():
    gateway, sock, *_ = build([event(10, {"heartbeat_interval": 500}), ready()])
    gateway._connection("wss://gateway.discord.gg")
    assert len([row for row in sock.sent if row["op"] == 1]) == 1


@pytest.mark.parametrize("code", [4004, 4010, 4011, 4012, 4013, 4014])
def test_fatal_setup_codes_do_not_reconnect_or_expose_close_reason(code):
    gateway, sock, *_ = build([(websocket.ABNF.OPCODE_CLOSE, struct.pack("!H", code) + b"fictional-private-reason")])
    with pytest.raises(DiscordUnavailable) as error:
        gateway.run()
    assert "fictional-private" not in str(error.value)
    assert sock.closed


@pytest.mark.parametrize("frame", [event(9, False), (websocket.ABNF.OPCODE_CLOSE, struct.pack("!H", 4009))])
def test_invalid_sessions_clear_resume_state(frame):
    gateway, *_ = build([frame])
    gateway.session_id, gateway.resume_url, gateway.sequence = "old", "wss://gateway.discord.gg", 4
    gateway._connection(gateway.resume_url)
    assert gateway.session_id is gateway.resume_url is gateway.sequence is None


def test_ready_must_belong_to_the_configured_bot_application():
    gateway, *_ = build([event(0, {"application": {"id": "999"}, "user": {"bot": True}}, t="READY")])
    with pytest.raises(DiscordUnavailable, match="different application"):
        gateway._connection("wss://gateway.discord.gg")
