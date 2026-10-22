"""Outbound Discord connection; heartbeats never wait for a save or model call."""
from __future__ import annotations

import json
import random
import struct
import threading
import time

import websocket

from .client import DiscordUnavailable, gateway_address

_FATAL_CODES = frozenset({4004, 4010, 4011, 4012, 4013, 4014})


class _ResumeSocket(websocket.WebSocket):
    def send_close(self, status=4000, reason=b""):
        # websocket-client otherwise replies to a server close with 1000,
        # which tells Discord to discard the very session we plan to resume.
        return super().send_close(status=status, reason=reason)


class Gateway:
    def __init__(self, client, on_event, *, application_id, read_threads=False,
                 stop=None, clock=time.monotonic, socket_factory=websocket.create_connection, jitter=random.random):
        self.client = client
        self.on_event = on_event
        self.application_id = application_id
        self.intents = (1 << 9) | (1 << 15) if read_threads else 0
        self.stop = stop or threading.Event()
        self.clock = clock
        self.socket_factory = socket_factory
        self.jitter = jitter
        self.session_id = self.resume_url = self.sequence = None
        self.ready = threading.Event()
        self._socket = None

    def _reset_session(self):
        self.session_id = self.resume_url = self.sequence = None

    def close(self):
        self.stop.set()
        if self._socket is not None:
            self._socket.close()

    def run(self):
        # The library's trace includes Identify credentials; never enable it.
        websocket.enableTrace(False)
        gateway = self.client.gateway()
        url = gateway["url"]
        limit = gateway.get("session_start_limit", {})
        if limit.get("remaining", 1) <= 0:
            raise DiscordUnavailable("Discord's connection limit needs time to reset")
        remaining = limit.get("remaining", 1)
        reset_window = limit.get("reset_after", 0) / 1000
        reset_at = self.clock() + reset_window if reset_window > 0 else float("inf")
        total = limit.get("total", remaining)
        failures = 0
        while not self.stop.is_set():
            if self.session_id is None:
                if self.clock() >= reset_at:
                    remaining = total
                    reset_at = self.clock() + reset_window
                if remaining <= 0:
                    raise DiscordUnavailable("Discord's connection limit needs time to reset")
                remaining -= 1
            try:
                self._connection(self.resume_url or url)
                failures = 0 if self.ready.is_set() else failures + 1
            except (OSError, websocket.WebSocketException):
                failures += 1
            finally:
                self.ready.clear()
                if self._socket is not None:
                    self._socket.close(status=1000 if self.stop.is_set() else 4000, timeout=0)
                    self._socket = None
            # A reconnect is transport recovery, never a replay of a save/send.
            self.stop.wait(min(30, max(5, 2 ** min(failures, 5))))

    def _connection(self, url):
        sock = self.socket_factory(gateway_address(url), timeout=10, enable_multithread=True,
                                   http_no_proxy=["discord.gg", ".discord.gg"],
                                   redirect_limit=0, class_=_ResumeSocket)
        self._socket = sock
        if sock.getstatus() != 101:
            raise DiscordUnavailable("Discord did not complete its gateway handshake")
        sock.settimeout(0.2)
        interval = next_beat = None
        awaiting = False

        def send(op, data):
            sock.send(json.dumps({"op": op, "d": data}, separators=(",", ":")))

        while not self.stop.is_set():
            if interval is not None and self.clock() >= next_beat:
                if awaiting:
                    return
                send(1, self.sequence)
                awaiting = True
                next_beat = self.clock() + interval
            try:
                opcode, raw = sock.recv_data(control_frame=True)
            except websocket.WebSocketTimeoutException:
                continue
            if opcode == websocket.ABNF.OPCODE_CLOSE:
                code = struct.unpack("!H", raw[:2])[0] if len(raw) >= 2 else 1000
                if code in _FATAL_CODES:
                    raise DiscordUnavailable("Discord connection setup was refused; operator inspection is required")
                if code in {4007, 4009}:
                    self._reset_session()
                return
            if opcode in {websocket.ABNF.OPCODE_PING, websocket.ABNF.OPCODE_PONG}:
                continue
            if opcode != websocket.ABNF.OPCODE_TEXT or len(raw) > 1_048_576:
                raise DiscordUnavailable("Discord returned an unsupported gateway message")
            try:
                event = json.loads(raw)
                op, data = event["op"], event.get("d")
            except (TypeError, ValueError, KeyError):
                raise DiscordUnavailable("Discord returned an unreadable gateway message") from None
            if event.get("s") is not None:
                self.sequence = event["s"]
            if op == 10:
                try:
                    interval = float(data["heartbeat_interval"]) / 1000
                    if not 0 < interval <= 300:
                        raise ValueError()
                except (TypeError, ValueError, KeyError):
                    raise DiscordUnavailable("Discord returned an invalid heartbeat interval") from None
                next_beat = self.clock() + self.jitter() * interval
                if self.session_id:
                    send(6, {"token": self.client._token, "session_id": self.session_id, "seq": self.sequence})
                else:
                    send(2, {"token": self.client._token, "intents": self.intents,
                             "properties": {"os": "linux", "browser": "exosuit-review", "device": "exosuit-review"}})
            elif op == 11:
                awaiting = False
            elif op == 1:
                send(1, self.sequence)
                awaiting = True
            elif op == 7:
                return
            elif op == 9:
                if data is not True:
                    self._reset_session()
                return
            elif op == 0:
                name = event.get("t")
                if name == "READY":
                    if (not isinstance(data, dict) or data.get("application", {}).get("id") != self.application_id
                            or data.get("user", {}).get("bot") is not True):
                        raise DiscordUnavailable("Discord connected a different application")
                    gateway_address(data.get("resume_gateway_url"))
                    self.session_id, self.resume_url = data["session_id"], data["resume_gateway_url"]
                    self.ready.set()
                elif name == "RESUMED":
                    self.ready.set()
                if name in {"READY", "RESUMED", "INTERACTION_CREATE", "MESSAGE_CREATE"}:
                    # Host callback enqueues on its owning async loop and returns.
                    # Exceptions are fatal rather than losing an owner's action.
                    self.on_event(name, data)
