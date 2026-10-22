"""Read Diablo's shared flagged-mail file; mail remains information, never Brain records."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Empty, Queue
from threading import Event, Thread
from time import monotonic

from brain_core.review import SECTION_CAP, HistoryRow, ImportantEmail
from brain_core.timewords import today_in_singapore
from gremlin_town.review.collect import SourceUnavailable

PING_THRESHOLD = 7  # Proposed: the monitor's ping bar.
RETENTION = timedelta(days=3)
MAX_LOG_BYTES = 4 * 1024 * 1024
EMAIL_REF_PREFIX = "__important_email__:"
MAIL_TIME_LIMIT_REASON = "mail check ran out of time"
MAIL_REQUEST_TIMEOUT = 2.0


class ImportantMailSource:
    """An operator-configured local path and the hub's authorized unread-label reader.

    Only delivered, capped Look rows consume mornings. A previously shown email
    can return on one further morning while retained and unread. Current-part
    reads on the same day do not consume another morning. No file is written.
    """

    def __init__(self, path: str | Path, is_unread: Callable[[str], bool] | None, *,
                 deadline_seconds: float = 10.0) -> None:
        self.path = Path(path)
        self.is_unread = is_unread
        self.deadline_seconds = deadline_seconds

    def __call__(self, now: datetime, previous_review_at: datetime | None,
                 history: tuple[HistoryRow, ...]) -> tuple[ImportantEmail, ...]:
        # Bound the whole collection, including file reads and token refresh. A
        # timed-out call cannot start more label checks or publish late results.
        deadline = monotonic() + self.deadline_seconds
        stopped = Event()
        replies: Queue[ImportantEmail | Exception | None] = Queue()

        def collect() -> None:
            try:
                for entry in self._collect(now, previous_review_at, history, stopped, deadline):
                    replies.put(entry)
            except Exception as exc:
                replies.put(exc)
            else:
                replies.put(None)

        Thread(target=collect, daemon=True, name="review-mail").start()
        result = []
        try:
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise Empty
                reply = replies.get(timeout=remaining)
                if reply is None:
                    return tuple(result)
                if isinstance(reply, SourceUnavailable) and reply.reason == MAIL_TIME_LIMIT_REASON:
                    raise SourceUnavailable(MAIL_TIME_LIMIT_REASON, items=tuple(result)) from reply
                if isinstance(reply, Exception):
                    raise reply
                result.append(reply)
        except Empty:
            raise SourceUnavailable(MAIL_TIME_LIMIT_REASON, items=tuple(result)) from None
        finally:
            # Never join here: even a stuck provider must not hold delivery.
            stopped.set()

    def _collect(self, now: datetime, previous_review_at: datetime | None,
                 history: tuple[HistoryRow, ...], stopped: Event, deadline: float) -> Iterator[ImportantEmail]:
        try:
            with self.path.open("rb") as handle:
                raw = handle.read(MAX_LOG_BYTES + 1)
            if len(raw) > MAX_LOG_BYTES:
                raise ValueError("too large")
            data = json.loads(raw)
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("emails"), list):
                raise ValueError("invalid log")
            entries = tuple(_entry(row) for row in data["emails"])
            if len({entry.message_id for entry in entries}) != len(entries):
                raise ValueError("duplicate IDs")
        except (OSError, ValueError, TypeError, OverflowError) as exc:
            raise SourceUnavailable("flagged-mail log is missing, unreadable or invalid") from exc
        if self.is_unread is None:
            raise SourceUnavailable("hub's read-only Google mail login is not configured")
        shown: dict[str, set] = {}
        for row in history:
            if row.channel == "review" and row.rule == "important_email" and row.surfaced_at <= now:
                shown.setdefault(row.item_ref, set()).add(today_in_singapore(row.surfaced_at))
        today = today_in_singapore(now)
        checked = 0
        for entry in sorted(entries, key=lambda entry: (-entry.flagged_at.timestamp(), entry.message_id)):
            if stopped.is_set():
                return
            if monotonic() >= deadline:
                raise SourceUnavailable(MAIL_TIME_LIMIT_REASON)
            if not now - RETENTION <= entry.flagged_at <= now or entry.score < PING_THRESHOLD:
                continue
            mornings = shown.get(EMAIL_REF_PREFIX + entry.message_id, set())
            if len(mornings) >= 2 and today not in mornings:
                continue
            if previous_review_at is not None and entry.flagged_at <= previous_review_at and not mornings:
                continue
            if checked >= SECTION_CAP:
                raise SourceUnavailable(MAIL_TIME_LIMIT_REASON)
            checked += 1
            try:
                unread = self.is_unread(entry.message_id)
                if not isinstance(unread, bool):
                    raise ValueError("invalid unread result")
            except Exception as exc:
                # All-or-nothing: never disguise an unknown label as read or consume a morning.
                raise SourceUnavailable("couldn't check unread mail with the hub's read-only Google login") from exc
            if unread and not stopped.is_set():
                yield entry


def _entry(row) -> ImportantEmail:
    if (not isinstance(row, dict)
            or any(not isinstance(row.get(key), str) for key in
                   ("message_id", "sender", "subject", "reason", "link"))
            or not re.fullmatch(r"[A-Za-z0-9_-]+", row["message_id"])
            or row["link"] != f"https://mail.google.com/mail/u/0/#all/{row['message_id']}"
            or isinstance(row.get("score"), bool) or not isinstance(row.get("score"), int)
            or not 0 <= row["score"] <= 10
            or isinstance(row.get("flagged_at"), bool) or not isinstance(row.get("flagged_at"), (int, float))
            or not math.isfinite(row["flagged_at"])):
        raise ValueError("invalid flagged mail")
    return ImportantEmail(row["message_id"], row["sender"], row["subject"], row["score"], row["reason"],
                          row["link"], datetime.fromtimestamp(row["flagged_at"], UTC))
