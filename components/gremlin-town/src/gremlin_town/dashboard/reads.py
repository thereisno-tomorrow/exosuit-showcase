"""Fresh dashboard reads from injected, authorized hub adapters only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable

from brain_core.dashboard import attach_schedule, project_work
from brain_core import work
from brain_core.orientation import focus_project_ids
from brain_core.presentation import day_words
from brain_core.timewords import SINGAPORE, personal_today
from gremlin_town.flowsavvy.snapshot import ScheduleSnapshot
from gremlin_town.review.collect import SourceUnavailable


class DashboardUnavailable(Exception):
    """Sanitized read failure. Never include source exceptions or record text."""


@dataclass(frozen=True)
class DashboardSources:
    items: Callable
    calendar: Callable | None = None
    schedule: Callable | None = None
    history: Callable | None = None
    preferences: Callable | None = None
    ranking: str = "points"


@dataclass(frozen=True)
class DashboardReader:
    sources: DashboardSources

    def read(self, now: datetime, *, day=None, zone=None, calendar_day=False, prepared_index=None):
        """No cached records or provider calls during rendering. Each request reads anew."""
        display_zone = zone or SINGAPORE
        now = now.astimezone(display_zone)
        notices = []
        try:
            entries = tuple(self.sources.items())
        except Exception:
            raise DashboardUnavailable("Couldn't see your work. Try again.") from None

        def optional(reader, *args, message, default=(), preserve_items=False):
            if reader is None:
                notices.append(message)
                return default
            try:
                return reader(*args)
            except SourceUnavailable as exc:
                notices.append("Couldn't see all your calendars; this day may be incomplete"
                               if preserve_items and exc.reason == "partial" else message)
                return exc.items if preserve_items else default
            except Exception:
                notices.append(message)
                return default

        history = optional(self.sources.history, message="Couldn't see today's picks") if self.sources.history else ()
        preferences = optional(self.sources.preferences, message="Couldn't see your focus projects") if self.sources.preferences else ()
        today = (now.date() if calendar_day else personal_today(now)) if day is None else day
        def pick_day(row):
            if calendar_day:
                stated = getattr(row, "fields_used", {}).get("review_day")
                if stated:
                    from datetime import date
                    return date.fromisoformat(stated)
            return personal_today(row.responded_at or row.surfaced_at)
        picked = frozenset(row.item_ref for row in history if row.response == "picked_today"
                           and pick_day(row) == today)
        try:
            projection = project_work(entries, now, picked_today=picked, history=history,
                                      focus_projects=focus_project_ids(preferences), ranking=self.sources.ranking, today=today, prepared_index=prepared_index)
        except Exception:
            raise DashboardUnavailable("Couldn't read your work. Try again.") from None
        snapshot = optional(self.sources.schedule, now, message="Couldn't see the schedule",
                            default=ScheduleSnapshot())
        available = bool(snapshot and snapshot.available)
        if not available and "Couldn't see the schedule" not in notices:
            notices.append("Couldn't see the schedule")
        slots = attach_schedule(projection, snapshot.blocks if available else (), now,
                                schedule_mode=self.sources.schedule is not None)
        # Legacy dashboard keeps its 4am boundary; the review opts into local midnight.
        # Calendar bounds stay explicit zoned instants, independent of host zone.
        start = datetime.combine(today, time(0 if calendar_day else 4), display_zone)
        end = start + timedelta(days=1)
        calendar = optional(self.sources.calendar, start, end, message="Couldn't see your calendar",
                            preserve_items=True)
        events = [dict(title=event.title, start=event.start.astimezone(display_zone).isoformat(),
                       end=event.end.astimezone(display_zone).isoformat(), all_day=event.all_day,
                       type="event", kind="calendar", ref=None)
                  for event in calendar if event.end > start and event.start < end
                  and event.exosuit_role not in {"block", "slot"}]
        timeline = sorted([*events, *(slot for slot in slots
                                     if datetime.fromisoformat(slot["end"]) > start
                                     and datetime.fromisoformat(slot["start"]) < end)],
                          key=lambda slot: (datetime.fromisoformat(slot["start"]), datetime.fromisoformat(slot["end"]), slot["title"]))
        current = next((slot for slot in timeline if not slot.get("all_day")
                        and datetime.fromisoformat(slot["start"]) <= now < datetime.fromisoformat(slot["end"])), None)
        next_slot = next((slot for slot in timeline if not slot.get("all_day")
                          and datetime.fromisoformat(slot["start"]) > now), None)
        details = projection["details"]
        today_items = [card for card in projection["cards"] if card["column"] == "today"]
        needs = [card for card in details.values() if card["needs_length"]
                 and card["status"] not in {"ended", "parked"}
                 and not card["dates"].get("appointment")]
        moved = [dict(item=card, schedule=slot) for card in details.values() if card["for_today"]
                 for slot in card["schedule"][:1] if datetime.fromisoformat(slot["start"]) >= end]
        done = [card for card in details.values() if card["state"] == "done"
                and work.done_day_of(card["ref"], projection["index"]) == today]
        age = (f"Schedule updated {snapshot.fetched_at.astimezone(display_zone):%H:%M}"
               if available and snapshot.fetched_at else "Couldn't see the schedule")
        common = dict(updated_at=now.isoformat(), date=today.isoformat(), date_label=day_words(today),
                      notices=notices, schedule=dict(available=available, label=age,
                      fetched_at=snapshot.fetched_at.isoformat() if snapshot and snapshot.fetched_at else None,
                      age_seconds=snapshot.age_seconds if snapshot else None))
        day = dict(**common, timeline=timeline, now=current, next=next_slot,
                   timeline_start=start.isoformat(), timeline_end=end.isoformat(),
                   today=today_items, done_count=len(done), needs_length=needs,
                   due_today=projection["due_today"], due_tomorrow=projection["due_tomorrow"],
                   moved=moved,
                   moved_groups=[dict(label=label, items=rows) for label, rows in (
                       ("Moved to tomorrow", [row for row in moved if datetime.fromisoformat(row["schedule"]["start"]).date() == today + timedelta(days=1)]),
                       ("Scheduled for a later day", [row for row in moved if datetime.fromisoformat(row["schedule"]["start"]).date() > today + timedelta(days=1)])) if rows],
                   ranked=[details[ref] for ref in projection["ranked_refs"] if ref in details])
        return dict(day=day, work=dict(**common, columns=projection["columns"]), details=details)
