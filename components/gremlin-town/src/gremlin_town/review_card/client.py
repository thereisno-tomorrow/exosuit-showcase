"""Narrow Discord REST adapter. No response body, token or URL enters errors."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
import math
import logging
import os
from pathlib import Path
import re
import stat
import threading
import time
from urllib.parse import urlsplit

import httpx

API = "https://discord.com/api/v10"
_ID = re.compile(r"[0-9]{1,20}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_.-]{1,2048}\Z")
_ACK_SECONDS = 2.0


class _PrivateRoutes(logging.Filter):
    def filter(self, record):
        message = record.getMessage()
        return "/interactions/" not in message and "/webhooks/" not in message


# HTTPX otherwise logs full callback URLs at INFO, including Discord's token.
logging.getLogger("httpx").addFilter(_PrivateRoutes())


class DiscordUnavailable(RuntimeError):
    def __init__(self, reason="Discord could not be reached", *, uncertain=False, retry_after=None):
        super().__init__(reason)
        self.uncertain = uncertain
        self.retry_after = retry_after


def identifier(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Discord identifier is invalid")
    return value


def gateway_address(value):
    if not isinstance(value, str):
        raise DiscordUnavailable("Discord returned an invalid gateway address")
    parsed = urlsplit(value)
    if (parsed.scheme != "wss" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment
            or parsed.path not in ("", "/")
            or not (parsed.hostname == "gateway.discord.gg" or parsed.hostname.endswith(".discord.gg"))):
        raise DiscordUnavailable("Discord returned an invalid gateway address")
    return value.rstrip("/") + "/?v=10&encoding=json"


def _systemd_credential(path: Path, info) -> bool:
    """systemd uses root:root 0440 inside a private, read-only 0550 mount.

    Group bits on that managed mount do not imply a shared ordinary token file.
    Accept only the actual credential directory supplied by systemd, never a
    caller-selected directory or a writable lookalike.
    """
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if (not directory or path.parent != Path(directory)
            or len(path.parts) != 5 or path.parts[1:3] != ("run", "credentials")
            or not path.parent.name.endswith(".service")
            or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o440):
        return False
    if any(parent.is_symlink() for parent in path.parents):
        return False
    parent = path.parent.lstat()
    return (stat.S_ISDIR(parent.st_mode) and parent.st_uid == parent.st_gid == 0
            and stat.S_IMODE(parent.st_mode) == 0o550
            and bool(os.statvfs(path.parent).f_flag & os.ST_RDONLY))


def read_credential(path: Path):
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode)
                or (os.name != "nt" and info.st_mode & 0o077 and not _systemd_credential(path, info))):
            raise ValueError()
        value = path.read_text(encoding="utf-8").strip()
        if not _TOKEN.fullmatch(value):
            raise ValueError()
        return value
    except (OSError, ValueError, UnicodeError):
        raise DiscordUnavailable("The Discord credential file is unavailable or not private") from None


class DiscordClient:
    def __init__(self, token, *, http=None, clock=time.monotonic):
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise DiscordUnavailable("The Discord credential is invalid")
        self._token = token
        self._http = http or httpx.Client(timeout=10, follow_redirects=False, trust_env=False)
        self._clock = clock
        self._lock = threading.Lock()
        self._blocked_until = 0.0
        self._callbacks = ThreadPoolExecutor(max_workers=2, thread_name_prefix="discord-callback")
        self._callback_slots = threading.BoundedSemaphore(2)

    def close(self):
        self._callbacks.shutdown(wait=False, cancel_futures=True)
        self._http.close()

    def request(self, method, path, payload=None, *, callback=False):
        if not isinstance(path, str) or not path.startswith("/") or ".." in path or ":" in path or "\\" in path:
            raise ValueError("Discord route is invalid")
        if method not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
            raise ValueError("Discord method is invalid")
        # Interaction callbacks have their own limit and must never queue behind
        # an ordinary bot request. A callback failure authorizes no Brain write.
        with self._lock:
            delay = self._blocked_until - self._clock()
        if not callback and delay > 0:
            raise DiscordUnavailable("Discord is busy; try again shortly", retry_after=delay)
        try:
            response = self._http.request(method, API + path, json=payload,
                headers={} if callback or path.startswith("/webhooks/") else {"Authorization": "Bot " + self._token},
                timeout=2.0 if callback else 10.0)
        except httpx.HTTPError:
            raise DiscordUnavailable(uncertain=method != "GET") from None
        if response.status_code == 429:
            try:
                retry = float(response.headers.get("Retry-After") or response.json()["retry_after"])
                if not math.isfinite(retry) or retry < 0:
                    raise ValueError()
            except (ValueError, TypeError, KeyError):
                raise DiscordUnavailable("Discord returned an unreadable rate limit") from None
            if not callback:
                with self._lock:
                    self._blocked_until = max(self._blocked_until, self._clock() + retry)
            raise DiscordUnavailable("Discord is busy; try again shortly", retry_after=retry)
        if not 200 <= response.status_code < 300:
            reason = ("Discord could not verify the bot" if response.status_code == 401 else
                      "Discord did not permit this action" if response.status_code == 403 else
                      "Discord could not find the channel or message" if response.status_code == 404 else
                      "Discord did not accept this action")
            raise DiscordUnavailable(reason, uncertain=method != "GET" and response.status_code >= 500)
        if not callback and response.headers.get("X-RateLimit-Remaining") == "0":
            try:
                delay = float(response.headers["X-RateLimit-Reset-After"])
                if math.isfinite(delay) and delay >= 0:
                    # Conservative across routes; no early retries while a known
                    # limit is active. Never sleep inside a tap callback.
                    with self._lock:
                        self._blocked_until = max(self._blocked_until, self._clock() + delay)
            except (KeyError, ValueError):
                pass  # A missing optional header does not invalidate success.
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            raise DiscordUnavailable("Discord returned an unreadable reply", uncertain=method != "GET") from None

    def gateway(self):
        result = self.request("GET", "/gateway/bot")
        if not isinstance(result, dict):
            raise DiscordUnavailable("Discord returned an unreadable gateway reply")
        gateway_address(result.get("url"))
        return result

    def bot_user(self, application_id):
        result = self.request("GET", "/oauth2/applications/@me")
        if (not isinstance(result, dict) or result.get("id") != application_id
                or not isinstance(result.get("bot"), dict) or result["bot"].get("bot") is not True):
            raise DiscordUnavailable("Discord connected a different application")
        return identifier(result["bot"].get("id"))

    def callback(self, interaction, payload):
        key, token = identifier(interaction.get("id")), interaction.get("token")
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Discord interaction token is invalid")
        # HTTPX's timeout is per I/O phase. Bound the complete acknowledgement,
        # including every phase, and never queue more work behind a slow call.
        if not self._callback_slots.acquire(blocking=False):
            raise DiscordUnavailable("Discord could not acknowledge the tap in time")
        started = self._clock()
        try:
            future = self._callbacks.submit(self.request, "POST",
                f"/interactions/{key}/{token}/callback", payload, callback=True)
        except RuntimeError:
            self._callback_slots.release()
            raise DiscordUnavailable("Discord is shutting down") from None
        future.add_done_callback(lambda _: self._callback_slots.release())
        try:
            result = future.result(timeout=_ACK_SECONDS)
        except FutureTimeout:
            raise DiscordUnavailable("Discord could not acknowledge the tap in time", uncertain=True) from None
        if self._clock() - started >= _ACK_SECONDS:
            raise DiscordUnavailable("Discord could not acknowledge the tap in time", uncertain=True)
        return result

    def post(self, channel, payload, *, nonce):
        identifier(channel)
        if not isinstance(nonce, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,25}", nonce):
            raise ValueError("Discord delivery nonce is invalid")
        body = {**payload, "allowed_mentions": {"parse": []}, "nonce": nonce, "enforce_nonce": True}
        result = self.request("POST", f"/channels/{channel}/messages", body)
        if not isinstance(result, dict) or not _ID.fullmatch(str(result.get("id", ""))):
            raise DiscordUnavailable("Discord did not confirm its message", uncertain=True)
        return result

    def followup(self, interaction, application_id, text):
        token = interaction.get("token")
        if not isinstance(token, str) or not _TOKEN.fullmatch(token):
            raise ValueError("Discord interaction token is invalid")
        return self.request("POST", f"/webhooks/{identifier(application_id)}/{token}",
                            {"content": text, "flags": 64, "allowed_mentions": {"parse": []}})

    def edit(self, channel, message, payload):
        return self.request("PATCH", f"/channels/{identifier(channel)}/messages/{identifier(message)}",
                            {**payload, "allowed_mentions": {"parse": []}})

    def find_nonce(self, channel, nonce, *, bot_user_id):
        """Reconcile a possibly accepted send, never assume absence means failure."""
        result = self.request("GET", f"/channels/{identifier(channel)}/messages?limit=100")
        if not isinstance(result, list):
            raise DiscordUnavailable("Discord could not check its recent messages")
        found = [row for row in result if row.get("nonce") == nonce
                 and row.get("author", {}).get("id") == bot_user_id]
        if len(found) > 1:
            raise DiscordUnavailable("Discord delivery needs operator inspection")
        return found[0] if found else None

    def thread(self, channel, message):
        # A message supports one thread. Read it first so recovery never starts
        # a second conversation after a lost thread-creation response.
        path = f"/channels/{identifier(channel)}/messages/{identifier(message)}"
        current = self.request("GET", path)
        if (not isinstance(current, dict) or current.get("id") != message
                or current.get("channel_id") != channel):
            raise DiscordUnavailable("Discord could not verify the review message")
        existing = current.get("thread", {})
        if not isinstance(existing, dict):
            raise DiscordUnavailable("Discord returned an unreadable conversation")
        if existing.get("id"):
            return identifier(existing["id"])
        result = self.request("POST", path + "/threads", {"name": "Morning review · with Diablo", "auto_archive_duration": 1440})
        if not isinstance(result, dict):
            raise DiscordUnavailable("Discord did not confirm the conversation", uncertain=True)
        return identifier(result.get("id"))
