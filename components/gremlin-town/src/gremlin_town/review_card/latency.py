"""Per-tap timing without item text, identifiers, credentials or request bodies."""
from contextlib import contextmanager
from contextvars import ContextVar
import logging
import time
import uuid

logger = logging.getLogger(__name__)
action_name = ContextVar("review_card_action", default="unknown")
ACTIONS = frozenset('add answer_page anything apply_draft appointment arrival ask ask_send ask_words back begin_selector bound_form canceled capacity change classify confirm correction date dated_outcome day_more delegate done draft_page draft_save drop duplicate_add duplicate_attach duplicate_choose existing_steps finished first followup_disposition form_ready go_day hold inspect inspect_saved item_more length length_custom length_unknown more more_actions next no_show not_done not_yet note open_more park parts planning_cancel planning_details planning_menu planning_operation planning_portion planning_resource planning_shared planning_start planning_window portion_result portion_select progress refresh remove_plan rename requirement resume_review return_approve return_change return_discharge return_draft return_preview_page return_reason return_transfer reuse_addition review_addition review_choices save saved_page schedule_only schedule_page selector_choice selector_choose selector_continue selector_none selector_page selector_skip skip start steps_page summary_page timebox timezone toggle under undo_drop update wait waiting_item waiting_updates weekly weekly_next when'.split())
correlation = ContextVar("review_card_tap", default=None)
current_phase = ContextVar("review_card_phase", default="interaction")


def start_tap():
    action_name.set("unknown")
    return correlation.set(uuid.uuid4().hex)


def finish_tap(token):
    correlation.reset(token)


def mark_action(name):
    action_name.set(name if name in ACTIONS else "unknown")
    if correlation.get():
        logger.info("review_card tap=%s action=%s", correlation.get(), action_name.get())


def elapsed(name, seconds):
    if correlation.get():
        logger.info("review_card tap=%s action=%s phase=%s seconds=%.3f", correlation.get(), action_name.get(), name, seconds)


@contextmanager
def phase(name):
    started = time.monotonic()
    token = current_phase.set(name)
    try:
        yield
    finally:
        current_phase.reset(token)
        if correlation.get():
            elapsed(name, time.monotonic() - started)


def provider_elapsed(category, seconds):
    """One line per actual GitHub request; counts derive from fixed categories."""
    if category not in {"read_ref", "publish_ref", "read_commit", "create_commit", "read_tree",
                        "create_tree", "read_blob", "create_blob", "other_read", "other_write"}:
        category = "unknown"
    if correlation.get():
        logger.info("review_card tap=%s action=%s phase=github_%s parent=%s seconds=%.3f",
                    correlation.get(), action_name.get(), category, current_phase.get(), seconds)
