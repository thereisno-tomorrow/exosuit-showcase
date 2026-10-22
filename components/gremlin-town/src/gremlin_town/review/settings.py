"""Configuration for the `[review]` and `[orientation]` features: off unless explicitly enabled.

Mirrors `sources/google/settings.py`'s TOML-table style (reject unknown keys,
explicit defaults). See docs/product/steps/e-review-and-return.md §8 and
docs/product/steps/f-orientation.md §2, §8.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from brain_core.review import RANKING_POINTS, RANKINGS

ALLOWED_KEYS = frozenset({
    "enabled", "sender_participant", "delivery_participant", "project", "why_log_file", "gap_minutes", "gap_hours",
    "telegram_spool_dir", "handoff_notes", "sweep_participant", "names_at", "important_mail", "important_mail_log",
    "important_mail_deadline_seconds", "opening_deadlines", "past_appointment_done", "tidy", "new_lengths",
})
DEFAULT_SENDER_PARTICIPANT = "exosuit-review"
DEFAULT_DELIVERY_PARTICIPANT = "hermes-wa"
DEFAULT_GAP_MINUTES = 30
DEFAULT_GAP_HOURS = "08:00-21:00"
_GAP_HOURS = re.compile(r"^(\d{2}):(\d{2})-(\d{2}):(\d{2})$")


class ReviewSettingsError(ValueError):
    """The `[review]` table is malformed."""


def _parse_gap_hours(value: str) -> tuple[int, int]:
    match = _GAP_HOURS.fullmatch(value)
    if match is None:
        raise ReviewSettingsError("[review] gap_hours must look like HH:MM-HH:MM")
    start_hour, start_minute, end_hour, end_minute = (int(part) for part in match.groups())
    if start_minute != 0 or end_minute != 0:
        raise ReviewSettingsError("[review] gap_hours only supports whole-hour boundaries")
    if not (0 <= start_hour < end_hour <= 24):
        raise ReviewSettingsError("[review] gap_hours must have start before end, both 0-24")
    return start_hour, end_hour


@dataclass(frozen=True, slots=True)
class ReviewSettings:
    enabled: bool
    sender_participant: str
    delivery_participant: str
    project: str | None
    why_log_file: str | None
    gap_minutes: int
    gap_start_hour: int
    gap_end_hour: int
    # The folder the root-side Telegram courier posts from (step E spec §2; review/spool.py).
    # Unset (the default), nothing is written there and delivery is exactly as before.
    telegram_spool_dir: str | None = None
    # N12a (Ciel rebuild slice 2): the hub keeps the "where I stopped" note for handed-off
    # work from the moments poll. Off by default; Proposed - Nic to confirm.
    handoff_notes: bool = False
    # The weekly sweep (29 Sep 2026, Nic: "lets do it"): on Fridays, the day of the weekly review, the
    # 06:00 run also asks this participant (Fleet Boss) to sweep its workers for anything Nic would be
    # hurt to lose and save it to Brain Home as suggestions for "To sort". Unset (the default), no sweep.
    sweep_participant: str | None = None
    # Proposed: one separate pre-review request per thing at 05:30 Singapore time.
    names_at: str = "05:30"
    important_mail: bool = False
    important_mail_log: str | None = None
    important_mail_deadline_seconds: float = 10.0
    opening_deadlines: bool = False
    past_appointment_done: bool = False
    tidy: bool = False
    new_lengths: bool = False

    @classmethod
    def from_table(cls, table: dict[str, Any] | None) -> "ReviewSettings":
        """Build settings from a parsed `[review]` TOML table, or its absence."""

        if table is None:
            return cls(
                enabled=False,
                sender_participant=DEFAULT_SENDER_PARTICIPANT,
                delivery_participant=DEFAULT_DELIVERY_PARTICIPANT,
                project=None,
                why_log_file=None,
                gap_minutes=DEFAULT_GAP_MINUTES,
                gap_start_hour=8,
                gap_end_hour=21,
                handoff_notes=False,
            )
        if not isinstance(table, dict):
            raise ReviewSettingsError("[review] must be a table")
        unknown = set(table) - ALLOWED_KEYS
        if unknown:
            raise ReviewSettingsError(f"[review] has unknown keys: {sorted(unknown)}")

        enabled = table.get("enabled", False)
        if not isinstance(enabled, bool):
            raise ReviewSettingsError("[review] enabled must be a boolean")

        sender_participant = table.get("sender_participant", DEFAULT_SENDER_PARTICIPANT)
        if not isinstance(sender_participant, str) or not sender_participant:
            raise ReviewSettingsError("[review] sender_participant must be a non-empty string")

        delivery_participant = table.get("delivery_participant", DEFAULT_DELIVERY_PARTICIPANT)
        if not isinstance(delivery_participant, str) or not delivery_participant:
            raise ReviewSettingsError("[review] delivery_participant must be a non-empty string")

        project = table.get("project")
        if project is not None and (not isinstance(project, str) or not project):
            raise ReviewSettingsError("[review] project must be a non-empty string")

        why_log_file = table.get("why_log_file")
        if why_log_file is not None and (not isinstance(why_log_file, str) or not why_log_file):
            raise ReviewSettingsError("[review] why_log_file must be a non-empty string")

        gap_minutes = table.get("gap_minutes", DEFAULT_GAP_MINUTES)
        if isinstance(gap_minutes, bool) or not isinstance(gap_minutes, int) or gap_minutes <= 0:
            raise ReviewSettingsError("[review] gap_minutes must be a positive integer")

        gap_start_hour, gap_end_hour = _parse_gap_hours(table.get("gap_hours", DEFAULT_GAP_HOURS))

        telegram_spool_dir = table.get("telegram_spool_dir")
        if telegram_spool_dir is not None and (not isinstance(telegram_spool_dir, str) or not telegram_spool_dir):
            raise ReviewSettingsError("[review] telegram_spool_dir must be a non-empty string")

        if enabled and why_log_file is None:
            raise ReviewSettingsError("[review] enabled requires why_log_file")

        handoff_notes = table.get("handoff_notes", False)
        if not isinstance(handoff_notes, bool):
            raise ReviewSettingsError("[review] handoff_notes must be a boolean")
        if handoff_notes and not enabled:
            # The notes are kept from the moments poll and its why-log, which run only when enabled.
            raise ReviewSettingsError("[review] handoff_notes requires enabled")

        sweep_participant = table.get("sweep_participant")
        if sweep_participant is not None and (not isinstance(sweep_participant, str) or not sweep_participant):
            raise ReviewSettingsError("[review] sweep_participant must be a non-empty string")
        if sweep_participant is not None and not enabled:
            raise ReviewSettingsError("[review] sweep_participant requires enabled")

        names_at = table.get("names_at", "05:30")
        if not isinstance(names_at, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", names_at) or names_at >= "06:00":
            raise ReviewSettingsError("[review] names_at must be HH:MM before 06:00 Singapore time")

        important_mail = table.get("important_mail", False)
        if not isinstance(important_mail, bool):
            raise ReviewSettingsError("[review] important_mail must be a boolean")
        important_mail_log = table.get("important_mail_log")
        if important_mail_log is not None and (not isinstance(important_mail_log, str) or not important_mail_log.strip()):
            raise ReviewSettingsError("[review] important_mail_log must be a non-empty path")
        if important_mail and (not enabled or important_mail_log is None):
            raise ReviewSettingsError("[review] important_mail requires enabled and important_mail_log")
        mail_deadline = table.get("important_mail_deadline_seconds", 10.0)
        if (isinstance(mail_deadline, bool) or not isinstance(mail_deadline, (int, float))
                or not math.isfinite(mail_deadline) or mail_deadline <= 0):
            raise ReviewSettingsError("[review] important_mail_deadline_seconds must be a positive finite number")

        opening_deadlines = table.get("opening_deadlines", False)
        if not isinstance(opening_deadlines, bool):
            raise ReviewSettingsError("[review] opening_deadlines must be a boolean")
        past_appointment_done = table.get("past_appointment_done", False)
        if not isinstance(past_appointment_done, bool):
            raise ReviewSettingsError("[review] past_appointment_done must be a boolean")
        tidy = table.get("tidy", False)
        if not isinstance(tidy, bool):
            raise ReviewSettingsError("[review] tidy must be a boolean")
        new_lengths = table.get("new_lengths", False)
        if not isinstance(new_lengths, bool):
            raise ReviewSettingsError("[review] new_lengths must be a boolean")

        return cls(
            enabled=enabled,
            sender_participant=sender_participant,
            delivery_participant=delivery_participant,
            project=project,
            why_log_file=why_log_file,
            gap_minutes=gap_minutes,
            gap_start_hour=gap_start_hour,
            gap_end_hour=gap_end_hour,
            telegram_spool_dir=telegram_spool_dir,
            handoff_notes=handoff_notes,
            sweep_participant=sweep_participant,
            names_at=names_at,
            important_mail=important_mail,
            important_mail_log=important_mail_log,
            important_mail_deadline_seconds=float(mail_deadline),
            opening_deadlines=opening_deadlines,
            past_appointment_done=past_appointment_done,
            tidy=tidy,
            new_lengths=new_lengths,
        )


ORIENTATION_ALLOWED_KEYS = frozenset({"enabled", "ranking", "blocks", "schedule"})


class OrientationSettingsError(ValueError):
    """The `[orientation]` table is malformed."""


@dataclass(frozen=True, slots=True)
class OrientationSettings:
    """``ranking`` is how both asks and the 06:00 review's "Coming up" rank: ``"points"`` (the
    Ciel and Brain Home PRD §5, the default), or ``"rules"``, the nine rules, kept as the way back
    until a week of real use (Accepted - Nic, 28 Sep 2026: "Yes, keep a way back")."""

    enabled: bool
    ranking: str = RANKING_POINTS
    # Time blocks (PRD §4.3): read from the calendar, so off until Nic's Google consent (ROA-18).
    blocks: bool = False
    schedule: bool = False

    @classmethod
    def from_table(cls, table: dict[str, Any] | None, *, review_enabled: bool) -> "OrientationSettings":
        """Build settings from a parsed `[orientation]` TOML table, or its absence.

        `[orientation] enabled` requires `[review] enabled` (step F spec §2): orientation reads
        and writes the same why-log `review` does, so ``review_enabled`` is the caller's already-
        parsed `[review].enabled`, not a key this table carries itself.
        """

        if table is None:
            return cls(enabled=False)
        if not isinstance(table, dict):
            raise OrientationSettingsError("[orientation] must be a table")
        unknown = set(table) - ORIENTATION_ALLOWED_KEYS
        if unknown:
            raise OrientationSettingsError(f"[orientation] has unknown keys: {sorted(unknown)}")

        enabled = table.get("enabled", False)
        if not isinstance(enabled, bool):
            raise OrientationSettingsError("[orientation] enabled must be a boolean")
        if enabled and not review_enabled:
            raise OrientationSettingsError("[orientation] enabled requires [review] enabled")
        ranking = table.get("ranking", RANKING_POINTS)
        if ranking not in RANKINGS:
            raise OrientationSettingsError(f"[orientation] ranking must be one of {sorted(RANKINGS)}")
        blocks = table.get("blocks", False)
        if not isinstance(blocks, bool):
            raise OrientationSettingsError("[orientation] blocks must be a boolean")

        schedule = table.get("schedule", False)
        if not isinstance(schedule, bool):
            raise OrientationSettingsError("[orientation] schedule must be a boolean")
        return cls(enabled=enabled, ranking=ranking, blocks=blocks, schedule=schedule)


# `[placement]` (step I, the placement engine; build plan slice 4): off unless enabled = true, and
# plan-only until write = true. Needs `[review] enabled`: it reads the same why-log and sources.
PLACEMENT_ALLOWED_KEYS = frozenset({
    "enabled", "write", "buffer_after_meeting_minutes", "deep_focus_min_minutes", "deep_focus_block_min_minutes",
    "deep_focus_max_minutes", "split_min_minutes", "settle_after_minutes", "busy", "repair_hour",
})
# Each number's allowed range; the defaults are step I section 8's (Nic, 29 Sep 2026, where marked there).
_PLACEMENT_NUMBERS = {
    "buffer_after_meeting_minutes": (10, 0, 60),
    "deep_focus_min_minutes": (120, 0, 360),
    "deep_focus_block_min_minutes": (90, 15, 360),
    "deep_focus_max_minutes": (360, 0, 600),
    "split_min_minutes": (60, 15, 240),
    # Proposed - Nic to confirm (build plan open point 1): with no day_settled signal, the day
    # counts as settled this long after his last pick.
    "settle_after_minutes": (20, 1, 240),
    # Slice 7's daily repair runs once a day from this hour, Singapore time (Proposed - Nic to confirm).
    "repair_hour": (12, 0, 23),
}


class PlacementSettingsError(ValueError):
    """The `[placement]` table is malformed."""


@dataclass(frozen=True, slots=True)
class PlacementSettings:
    enabled: bool = False
    write: bool = False
    buffer_after_meeting_minutes: int = 10
    deep_focus_min_minutes: int = 120
    deep_focus_block_min_minutes: int = 90
    deep_focus_max_minutes: int = 360
    split_min_minutes: int = 60
    settle_after_minutes: int = 20
    busy: bool = True
    repair_hour: int = 12

    @classmethod
    def from_table(cls, table: dict[str, Any] | None, *, review_enabled: bool) -> "PlacementSettings":
        if table is None:
            return cls()
        if not isinstance(table, dict):
            raise PlacementSettingsError("[placement] must be a table")
        unknown = set(table) - PLACEMENT_ALLOWED_KEYS
        if unknown:
            raise PlacementSettingsError(f"[placement] has unknown keys: {sorted(unknown)}")
        values: dict[str, Any] = {}
        for key in ("enabled", "write", "busy"):
            if key in table:
                if not isinstance(table[key], bool):
                    raise PlacementSettingsError(f"[placement] {key} must be a boolean")
                values[key] = table[key]
        for key, (_default, low, high) in _PLACEMENT_NUMBERS.items():
            if key in table:
                value = table[key]
                if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                    raise PlacementSettingsError(f"[placement] {key} must be a whole number from {low} to {high}")
                values[key] = value
        settings = cls(**values)
        if settings.enabled and not review_enabled:
            raise PlacementSettingsError("[placement] enabled requires [review] enabled")
        if settings.deep_focus_min_minutes > settings.deep_focus_max_minutes:
            raise PlacementSettingsError("[placement] deep_focus_min_minutes must not exceed deep_focus_max_minutes")
        return settings
