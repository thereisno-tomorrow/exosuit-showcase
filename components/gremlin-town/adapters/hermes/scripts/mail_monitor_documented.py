"""Candidate monitor entrypoint using only the documented Hermes scorer command.

Install alongside mail_monitor.py and mail_scorer.py, change the cron script only
at the authorized upgrade. Existing delivery, cursor and fail-once behavior stay
in the original monitor. --check verifies Gmail and the isolated scorer policy,
never calls a model. No private classify_items import occurs on this entrypoint.
"""
from __future__ import annotations

import os
import sys
import urllib.parse
from pathlib import Path

import mail_monitor as monitor
import mail_scorer


def scorer_home() -> Path:
    value = os.environ.get("MAIL_SCORER_HOME")
    if not value:
        raise monitor.MonitorError("MAIL_SCORER_HOME must name the separate no-tool scorer home")
    return Path(value)


def scorer(items):
    try:
        return mail_scorer.score(items, home=scorer_home(), criteria=monitor.CRITERIA)
    except mail_scorer.ScorerError as exc:
        raise monitor.MonitorError(str(exc), cause="documented scorer") from None


def check(home, *, http=monitor.http_json):
    mail_scorer.validate_home(scorer_home())
    token = monitor.access_token(monitor.load_login(monitor.token_path(home)), http)
    body = http(f"{monitor.API_BASE}/messages?{urllib.parse.urlencode({'q': 'in:inbox newer_than:1d', 'maxResults': '100'})}", None, token)
    return f"Mail monitor check: exactly read-only login and inbox answered ({len(body.get('messages') or [])}); documented scorer policy checked. Nothing saved or sent."


def main(argv=None):
    original_run = monitor.run
    original_check = monitor.check
    def run(home, **kwargs):
        return original_run(home, scorer=scorer, **kwargs)
    monitor.run = run
    monitor.check = check
    try:
        return monitor.main(argv)
    except mail_scorer.ScorerError as exc:
        print(f"Mail monitor: {exc}", file=sys.stderr)
        return 1
    finally:
        monitor.run = original_run
        monitor.check = original_check


if __name__ == "__main__":
    sys.exit(main())
