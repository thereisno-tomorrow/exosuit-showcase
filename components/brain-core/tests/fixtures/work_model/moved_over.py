"""Hand-built expected move-over of W0's synthetic exhibition scenario (plan 9.2/9.3).

This is synthetic test output, not an audit of a live migration. Original files
are untouched. Closed records, routines and ideas retain their bytes/fields;
old versions remain as history. The Sam/pasta test records are omitted from the
active snapshot (their fixture files are not moved). Borderline records remain.
Stakes below are proposals awaiting the dry-run yes, never confirmed owner decisions.
W18 should compare semantic output with these hand-built expected work facts;
the deterministic fixture IDs/times are not promised production migration IDs.
"""

from __future__ import annotations

import importlib.util
from copy import deepcopy
from pathlib import Path

from brain_core.records import first_id, record_from_mapping

ROOT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location("work_model_w0_helpers", ROOT / "helpers.py")
assert _spec and _spec.loader
_helpers = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_helpers)

Exhibition = "brn_20260929_123414_d00000000000000f"
MEMO = "brn_20260930_035041_d000000000000029"
SUBMIT = "brn_20260930_035056_d00000000000002a"
SEND = "brn_20260930_020454_d000000000000022"
EDIT = "brn_20260930_035653_d00000000000002c"
CAMPAIGN = "brn_20260930_020457_d000000000000023"
Banner = "brn_20260929_134536_d000000000000011"
VENUE_RENTAL = "brn_20260929_105119_d000000000000008"
ExhibitCover = "brn_20260929_123409_d00000000000000e"
Printworks = "brn_20260929_123405_d00000000000000d"
ADMIN = "brn_20260929_105122_d000000000000009"
EMAIL = "brn_20260929_105126_d00000000000000a"
OFFICER = "brn_20260930_120012_000000000000e00c"


def _date(words: str, day: str, read_on: str = "2026-09-30", **extra) -> dict:
    return dict(words=words, day=day, read_by="code", read_on=read_on, **extra)


def _stake(level: str, at_stake: str | None = None, cost_shape: str | None = None) -> dict:
    result = dict(level=level, confirmed=False, proposed_by="fixture:dry-run-awaiting-yes")
    if at_stake:
        result["at_stake"] = at_stake
    if cost_shape:
        result["cost_shape"] = cost_shape
    return result


def moved_over_records() -> tuple:
    """Return fresh parsed Records: W0 history plus 12 expected versions and one part.

    The record provenance continues to say synthetic exhibition scenario, including
    the new officer part. No invented original owner attribution or evidence.
    """
    original = _helpers.load_records()
    test_ids = set(_helpers.manifest()["test_data_ids"])
    records = [r for r in original if r.id not in test_ids]
    current_ids = set(_helpers.manifest()["audited_current_ids"])
    current = {first_id(r): r for r in original if r.id in current_ids}
    # Explicit expectations, independent of the reader and future move-over code.
    changes = (
        # No finish line is invented: the first review asks the fictional organizer.
        (Exhibition, dict(labels=["exhibition admin"],
                        started_at="2026-09-30T03:50:00+00:00",
                        stake=_stake("hurts_weeks", "The exhibition would be delayed", "lands"),
                        review_marks=[dict(mark="question", text="Its finish line, in your words?")]), "open", None, True),
        (MEMO, dict(parent=Exhibition, order=2, dates={"not_before": _date(
            "8 October", "2026-10-08", event="venue inspection"), "planned": _date(
            "8 October", "2026-10-08", event="venue inspection")}), "open", "Get a safety checklist from the inspector", False),
        (SUBMIT, dict(parent=Exhibition, order=3, after=[MEMO]), "open",
         "Officially submit my permit along with the memo on Exhibit Portal", False),  # M01's exact phrase
        (SEND, dict(dates={"planned": _date("today", "2026-09-30")}, review_marks=[dict(
            mark="question", text="Is it a project with 'Plan the Atlas outreach campaign'? Its name?")]), "open", None, False),
        (EDIT, dict(parent=SEND, order=1, dates={"planned": _date("today", "2026-09-30")}),
         "open", "Edit my brief", False),  # M04's exact phrase (memo 2.15)
        (CAMPAIGN, dict(dates={"planned": _date("today", "2026-09-30")}), "open", None, False),
        (Banner, dict(dates={"appointment": _date("at 12pm", "2026-09-30", "2026-09-29", at="12:00")},
                     review_marks=[dict(mark="question", text="Did it happen?")]), "open", None, False),  # plan 9.3
        (VENUE_RENTAL, dict(labels=["exhibition admin"], stake=_stake("changes_life", "about 40 display slots a month", "grows"),
                       review_marks=[dict(mark="question", text="Exhibition admin's list names three parts: a venue appointment, a inspector's approval, and the co-organizer's signature. Yes, or say them your way.")]), "open", None, True),
        (ExhibitCover, dict(labels=["exhibition admin"], stake=_stake("nothing_much")), "open", None, False),
        (Printworks, dict(labels=["exhibition admin"], stake=_stake("nuisance")), "open", None, False),
        (ADMIN, dict(reason="Just a label"), "no_longer_needed", None, False),
        (EMAIL, dict(stake=_stake("nothing_much"), review_marks=[dict(mark="question", text="Park it, or what's next?")]), "open", None, False),
    )
    for pos, (key, fields, state, title, remove_length) in enumerate(changes):
        old = current[key]
        value = deepcopy(old.as_dict())
        data = value["data"]
        data["size"] = "task"
        data.pop("first_step", None)
        data.pop("importance", None)
        # All structural old links on these things are explicitly replaced below.
        value["links"] = [link for link in value.get("links", ()) if link["type"] not in {"part_of", "blocked_by", "waiting_on"}]
        timing = data.get("timing") or {}
        data["timing"] = {k: v for k, v in timing.items() if k in {"duration_minutes", "padded_minutes", "estimated_by"} and not remove_length}
        data.update(fields)
        stamp = f"2026-09-30T12:00:{pos:02d}+00:00"
        value.update(id=f"brn_20260930_1200{pos:02d}_{0xe000 + pos:016x}",
                     created_at=stamp, updated_at=stamp, state=state,
                     supersedes=[*old.supersedes, old.id])
        if title:
            value["title"] = title
        records.append(record_from_mapping(value, "hand-built expected move-over of W0 reconstruction"))
    officer = deepcopy(current[Exhibition].as_dict())
    officer.update(id=OFFICER, title="Text my coordinator", state="done", supersedes=[], links=[],
                   created_at="2026-09-30T12:00:12+00:00", updated_at="2026-09-30T12:00:12+00:00",
                   data=dict(level="accepted", size="task", parent=Exhibition, order=1,
                             dates=dict(done=_date("I basically texted my coordinator", "2026-09-30")),
                             check=dict(locator="messages_30_sep.json#M01 (synthetic exhibition scenario)",
                                        checked_by="fixture:synthetic-exhibition"),
                             reason="I basically texted my coordinator"),
                   body="I basically texted my coordinator\nActual tick time unknown; version timestamps are migration timestamps."
                        "\nDone day comes from source words and time.")
    records.append(record_from_mapping(officer, "hand-built officer part; no live tick-time evidence"))
    return tuple(records)
