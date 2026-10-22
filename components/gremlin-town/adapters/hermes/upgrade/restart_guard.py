"""Read-only reconstructed mega restart guard. Never calls Docker or SSH.

The hub owner captures an idle observation and new log tail privately; this
helper validates markers without printing log content. Actual restart remains
an explicit authorized step in the runbook, separate from upgrade_check.
"""
from __future__ import annotations

from datetime import datetime, timezone


def eligible(observation: dict, now: datetime) -> bool:
    required = {"container", "mount", "observed_at", "active_turns", "active_jobs", "ready"}
    if set(observation) != required:
        return False
    try:
        age = (now - datetime.fromisoformat(observation["observed_at"])).total_seconds()
    except (ValueError, TypeError):
        return False
    return (0 <= age <= 60 and observation["container"] == "hermes-wa"
            and observation["mount"] == "/srv/hermes-example"
            and type(observation["active_turns"]) is int and observation["active_turns"] == 0
            and type(observation["active_jobs"]) is int and observation["active_jobs"] == 0
            and observation["ready"] is True)


def connections(lines: list[dict], started_at: datetime, required: dict[str, str]) -> dict[str, bool]:
    """Input is only bytes appended after the independently captured log offset.

    Each line carries its Docker UTC timestamp and parsed connection message.
    A caller passing old bytes must not count them. No retry or repair occurs.
    """
    answer = {platform: False for platform in required}
    for line in lines:
        try:
            timestamp = datetime.fromisoformat(line["timestamp"])
            if timestamp.tzinfo is None or timestamp < started_at:
                continue
            for platform, marker in required.items():
                if marker in line["message"]:
                    answer[platform] = True
        except (KeyError, TypeError, ValueError):
            continue  # Malformed observations never contribute a passing marker.
    return answer
