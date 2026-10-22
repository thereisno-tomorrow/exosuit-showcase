"""Translate a small set of owner taps into the existing checked work door.

The browser cannot submit moves, caller identity, scope, or source attribution.
Button labels describe the selected action; typed text is kept verbatim.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from gremlin_town.mcp.brain_runtime import BrainCaller, OwnerSelector, WorkRequest
from gremlin_town.mcp.surface_door import DASHBOARD_CALLER


class InvalidAction(ValueError):
    pass


_KEY = re.compile(r"[a-z0-9][a-z0-9-]{0,127}\Z")
_HASH = re.compile(r"[a-f0-9]{64}\Z")
_ACTIONS = {
    "tick": set(), "untick": set(), "today": set(), "can_do_now": set(),
    "park": {"day"}, "note": {"text"}, "steps": {"text"},
    "quick_add": {"text"}, "length": {"minutes"}, "undo": {"change"},
}


def _text(value: Any, label: str, limit: int = 8192) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise InvalidAction(f"{label} needs text of at most {limit} characters.")
    return value


def _key(value: Any, label: str) -> str:
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise InvalidAction(f"{label} is not valid. Refresh and try again.")
    return value


def split_steps(text: str) -> list[dict[str, Any]]:
    """Keep owner wording; 'then' alone adds an explicit dependency."""
    tokens = re.split(r"(\bthen\b)|[\r\n,]+", text, flags=re.IGNORECASE)
    parts: list[dict[str, Any]] = []
    ordered = False
    for token in tokens:
        if token is None or not token.strip():
            continue
        if token.casefold() == "then":
            ordered = True
            continue
        title = token.strip()
        _text(title, "Each step", 256)
        part: dict[str, Any] = {"title": title}
        if ordered and parts:
            part["then"] = True
        parts.append(part)
        ordered = False
    if not 1 <= len(parts) <= 20:
        raise InvalidAction("Add between one and twenty steps.")
    return parts


def compile_action(payload: Any) -> WorkRequest:
    if not isinstance(payload, dict):
        raise InvalidAction("Choose an action on the item.")
    action = payload.get("action")
    if not isinstance(action, str) or action not in _ACTIONS:
        raise InvalidAction("That action is not available.")
    required = {"action", "operation_id", *_ACTIONS[action]}
    if action != "quick_add":
        required |= {"ref", "expected"}
    if set(payload) != required:
        raise InvalidAction("The action has missing or unexpected fields. Refresh and try again.")
    operation_id = _key(payload["operation_id"], "The retry key")
    expected = None
    target: dict[str, Any] = {}
    if action != "quick_add":
        ref = _text(payload["ref"], "The item", 128)
        proof = payload["expected"]
        if not isinstance(proof, dict) or set(proof) != {"record_id", "sha256"}:
            raise InvalidAction("Refresh this item before changing it.")
        record_id = _text(proof["record_id"], "The item version", 128)
        digest = proof["sha256"]
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise InvalidAction("Refresh this item before changing it.")
        expected = {ref: {"record_id": record_id, "sha256": digest}}
        target = {"thing": ref}

    if action == "tick":
        message, move = "Mark done", {"move": "tick", **target}
    elif action == "untick":
        message, move = "Mark not done", {"move": "organise", **target, "reopen": True}
    elif action == "today":
        message, move = "Do this first today", {"move": "date", **target, "when": "today", "kind": "planned", "unpark": True}
    elif action == "can_do_now":
        message, move = "Can do now", {"move": "organise", **target, "can_do_now": True}
    elif action == "park":
        day = _text(payload["day"], "The day", 128)
        if day == "Someday":
            message, move = "Park until Someday", {"move": "park", **target, "event": "Someday"}
        else:
            # A selected calendar day is an exact owner selection, not a guessed date.
            from datetime import date
            try:
                if date.fromisoformat(day).isoformat() != day:
                    raise ValueError
            except ValueError as exc:
                raise InvalidAction("Choose a calendar day or Someday.") from exc
            message, move = f"Park until {day}", {"move": "park", **target, "until": day}
    elif action == "note":
        text = _text(payload["text"], "The note")
        message, move = text, {"move": "organise", **target, "note": text}
    elif action == "steps":
        text = _text(payload["text"], "The steps")
        message, move = text, {"move": "parts", **target, "parts": split_steps(text)}
    elif action == "quick_add":
        text = _text(payload["text"], "The new thing", 256)
        message, move = text, {"move": "new", "title": text, "review": True}
    elif action == "length":
        minutes = payload["minutes"]
        if type(minutes) is not int or not 1 <= minutes <= 525600:
            raise InvalidAction("Choose a length in whole minutes.")
        message = f"Set length: {minutes} min"
        move = {"move": "organise", **target, "rough_minutes": minutes, "length_is_nics": True}
    else:
        change = _key(payload["change"], "The saved change")
        message, move = "Undo this change", {"move": "undo", "change": change}
    return WorkRequest(operation_id, message, "owner", "direct", (move,), expected_records=expected)


class DashboardWriter:
    def __init__(self, brain_runtime):
        self.brain_runtime = brain_runtime

    async def change(self, payload: Mapping[str, Any], *, owner_id: str):
        request = compile_action(payload)
        return await self.brain_runtime.change_work_from_tap(
            BrainCaller(DASHBOARD_CALLER, frozenset({"bc:read", "bc:write"})),
            OwnerSelector("personal"), request, authenticated_owner_id=owner_id,
        )
