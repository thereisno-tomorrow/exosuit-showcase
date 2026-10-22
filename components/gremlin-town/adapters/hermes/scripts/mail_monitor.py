"""Diablo's important-mail monitor (ROA-91), run by a script-only Hermes cron job (`--no-agent`).

Each run: inbox mail that arrived since the last good run goes to Hermes's own urgency scorer
(`cron.scripts.classify_items`, the Important-mail monitor's rule word for word), and every email
scoring 7 or more is printed. Hermes sends what is printed to Nic's Telegram DM as-is; printing
nothing sends nothing. The separately installed, default-off Discord route first tries native
Hermes delivery there; a confirmed send prints nothing, otherwise Telegram remains the fallback.

A failure is never silent the first time: it exits non-zero, which Hermes delivers as an error alert.
The job runs every 3 minutes (Nic, 29 Sep), so the same failure again stays quiet (exit 0, nothing
printed) until a run succeeds, which then sends "Mail monitor is working again". A different failure
alerts again. The failure is kept in its own file beside the state; the mail cursor never moves on a
failed run.

Two things Hermes's own catalog job doesn't do:
- an email the scorer gave no readable score is an error, not a quiet skip;
- an email counts as seen only after a run that scored it, so a failed run tries it again.

Read-only by construction: the separate Gmail login (`$HERMES_HOME/google_token_mail.json`,
overridable with `MAIL_MONITOR_TOKEN_PATH`, written by `scripts/google_consent.py --for diablo-mail`)
must hold `gmail.readonly` and nothing else, checked
against what Google says on every refresh. The token file is read, never written. Standard library
only: Hermes runs this with its own Python, which has no Google client installed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict

GMAIL_READ_ONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
GMAIL_LINK = "https://mail.google.com/mail/u/0/#all/{message_id}"

# Hermes v0.20.5 `cron/suggestion_catalog.py`, "Important-mail monitor", word for word (Nic, 29 Sep:
# "follow the hermes default"). Its threshold is 7 of 10.
RULE = (
    "surface only mail that needs a reply today, is from a manager/family member, or mentions a deadline"
)
THRESHOLD = 7
# Hermes v0.20.5 `cron/scripts/classify_items.py`, `_CLASSIFY_INSTRUCTIONS`, word for word. Hermes defines
# it but never sends it (`_build_prompt` sends only the criteria and the items), so the model guesses the
# answer's shape and `_parse_scores` drops every entry without "index" (live, 29 Sep: GPT-6 Luna answered
# [{"score": 5}], all 5 emails unscored). The monitor sends it inside the criteria; if a later Hermes sends it
# too, the repeat is harmless.
FORMAT = (
    "You are an urgency classifier for a proactive assistant. You will be given a numbered list of items "
    "and the user's importance criteria. Score EACH item from 0 (ignore entirely) to 10 (interrupt the user "
    "now). Return ONLY a JSON array, one object per item, in the same order: "
    '[{"index": <int>, "score": <int 0-10>, "reason": "<short>"}]. '
    "No prose, no markdown fences. Be conservative: most items should score low. "
    "Only score high when the item clearly meets the user's criteria."
)
CRITERIA = RULE + "\n\n" + FORMAT
# Hermes's scorer answers in at most 1,024 tokens; ten emails fit with room to spare.
BATCH = 10
# At most this many new emails are scored per run; the rest wait for the next run.
MAX_PER_RUN = 30
# Looked-up window reaches this far before the last good run, for mail Google files late.
OVERLAP_SECONDS = 3600
# Seen IDs are kept this long, well past the overlap.
SEEN_SECONDS = 3 * 24 * 3600
FLAGGED_SECONDS = 3 * 24 * 3600
FLAGGED_LOG_NAME = "flagged-mail.json"
LIST_CAP = 500
TIMEOUT = 20.0
SCORER_TIMEOUT = 300.0
PING_CHARS = 450  # Telegram replies quote the first 500 characters back to Diablo

Http = Callable[[str, "dict[str, str] | None", "str | None"], dict[str, Any]]
Scorer = Callable[[list[dict[str, Any]]], list[dict[str, Any]]]


class FlaggedMail(TypedDict):
    """One scored email in the short-lived flagged-mail log beside monitor state."""

    message_id: str
    sender: str
    subject: str
    score: int
    reason: str
    link: str
    flagged_at: float


class MonitorError(RuntimeError):
    """A run failed; the message says what broke and never carries a secret.

    ``cause`` names the kind of failure for alert-once: the same cause on the next run stays quiet.
    By default it is the message with its numbers blanked, so "1 of 2" and "3 of 5" count as one."""

    def __init__(self, message: str, cause: str | None = None) -> None:
        super().__init__(message)
        self.cause = cause or re.sub(r"\d+", "#", message)


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or "/opt/data")


def token_path(home: Path) -> Path:
    """The monitor's own login; never fall back to the agent's google_token.json."""

    override = os.environ.get("MAIL_MONITOR_TOKEN_PATH")
    return Path(override).expanduser() if override else home / "google_token_mail.json"


# ---------------------------------------------------------------------------- Gmail


def load_login(path: Path) -> dict[str, str]:
    """The client ID, client secret, refresh token and token URI from the monitor's login file."""

    if not path.exists():
        raise MonitorError(
            f"No Gmail login at {path}. Run scripts/google_consent.py --for diablo-mail on Nic's PC "
            "and install the separate monitor file (docs/operations.md, 'Diablo Gmail edit access')"
        )
    text = ""
    for attempt in range(2):
        text = path.read_text(encoding="utf-8")
        try:
            data = json.loads(text)
            break
        except json.JSONDecodeError:
            if attempt:  # the operator may be replacing the login file this very moment
                raise MonitorError(f"The Gmail login at {path} isn't valid JSON") from None
            time.sleep(1)
    if not isinstance(data, dict):
        raise MonitorError(f"The Gmail login at {path} isn't a login file")
    login = {field: data.get(field) for field in ("client_id", "client_secret", "refresh_token")}
    for field, value in login.items():
        if not isinstance(value, str) or not value:
            raise MonitorError(f"The Gmail login at {path} has no {field}")
    token_uri = data.get("token_uri")
    login["token_uri"] = token_uri if isinstance(token_uri, str) and token_uri else DEFAULT_TOKEN_URI
    return login  # type: ignore[return-value]


def http_json(url: str, data: dict[str, str] | None = None, token: str | None = None) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("ascii") if data is not None else None
    request = urllib.request.Request(url, data=body)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    where = url.split("?")[0]
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Google's error bodies carry a code and a description, never the secret sent.
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            detail = {}
        error = detail.get("error_description") or detail.get("error")
        if isinstance(error, dict):
            error = error.get("message") or error.get("status")
        raise MonitorError(
            f"Google answered {exc.code} for {where}: {error or exc.reason}", cause=f"google {exc.code} {where}"
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MonitorError(f"Couldn't reach {where}: {exc}", cause=f"unreachable {where}") from exc
    except ValueError as exc:
        raise MonitorError(f"Google's answer from {where} wasn't JSON", cause=f"not json {where}") from exc
    if not isinstance(answer, dict):
        raise MonitorError(f"Google's answer from {where} wasn't a JSON object")
    return answer


def access_token(login: dict[str, str], http: Http) -> str:
    """A fresh access token, refused unless Google says this login may only read mail."""

    answer = http(login["token_uri"], {
        "client_id": login["client_id"],
        "client_secret": login["client_secret"],
        "refresh_token": login["refresh_token"],
        "grant_type": "refresh_token",
    }, None)
    token = answer.get("access_token")
    if not isinstance(token, str) or not token:
        raise MonitorError("Google's token answer had no access token")
    granted = set(str(answer.get("scope", "")).split())
    if granted != {GMAIL_READ_ONLY_SCOPE}:
        raise MonitorError(
            "The mail monitor's Gmail login must be read-only (gmail.readonly and nothing else); Google says it "
            f"holds: {', '.join(sorted(granted)) or 'nothing'}. Redo the consent with --for diablo-mail"
        )
    return token


def new_message_ids(token: str, after: int, http: Http) -> list[str]:
    """Inbox message IDs Gmail files after ``after`` (epoch seconds), newest first."""

    ids: list[str] = []
    page: str | None = None
    while len(ids) < LIST_CAP:
        params = {"q": f"in:inbox after:{after}", "maxResults": "100"}
        if page:
            params["pageToken"] = page
        body = http(f"{API_BASE}/messages?{urllib.parse.urlencode(params)}", None, token)
        for entry in body.get("messages") or []:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]:
                ids.append(entry["id"])
        page = body.get("nextPageToken") if isinstance(body.get("nextPageToken"), str) else None
        if not page:
            break
    return ids[:LIST_CAP]


def message_item(message_id: str, token: str, http: Http) -> dict[str, Any]:
    """Sender, subject and Gmail's snippet: what the scorer sees. No body, no attachments."""

    query = urllib.parse.urlencode(
        [("format", "metadata"), ("metadataHeaders", "From"), ("metadataHeaders", "Subject")]
    )
    body = http(f"{API_BASE}/messages/{urllib.parse.quote(message_id)}?{query}", None, token)
    headers: dict[str, str] = {}
    payload = body.get("payload")
    if isinstance(payload, dict):
        for header in payload.get("headers") or []:
            if isinstance(header, dict) and isinstance(header.get("value"), str):
                headers[str(header.get("name", "")).lower()] = header["value"]
    snippet = body.get("snippet")
    return {
        "id": message_id,
        "from": headers.get("from", "(no sender)"),
        "subject": headers.get("subject", "(no subject)"),
        "summary": snippet if isinstance(snippet, str) else "",
        "url": GMAIL_LINK.format(message_id=message_id),
    }


# ---------------------------------------------------------------------------- scoring


def hermes_scorer(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Hermes's own scorer over one batch, told the rule and the answer format (`CRITERIA`). Threshold 0
    so every score comes back and a missing one shows; the real threshold is applied in `score_all`."""

    command = [
        sys.executable, "-m", "cron.scripts.classify_items",
        "--criteria", CRITERIA, "--threshold", "0", "--format", "json",
    ]
    try:
        done = subprocess.run(
            command, input=json.dumps(items), capture_output=True, text=True,
            encoding="utf-8", timeout=SCORER_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MonitorError(f"Hermes's scorer took longer than {int(SCORER_TIMEOUT)} s", cause="scorer timeout") from exc
    if done.returncode != 0:
        raise MonitorError(
            f"Hermes's scorer failed (exit {done.returncode}): {done.stderr.strip()[-500:] or 'no message'}",
            cause=f"scorer exit {done.returncode}",
        )
    if not done.stdout.strip():
        if items:  # at threshold 0 every scored item is printed, so nothing printed means nothing scored
            raise MonitorError(
                f"Hermes's scorer gave no readable score for any of {len(items)} emails; none of them was marked "
                f"seen, so the next run tries again. Scorer said: {done.stderr.strip()[-300:] or 'nothing'}",
                cause="scorer gave no scores",
            )
        return []
    try:
        answer = json.loads(done.stdout)
    except ValueError as exc:
        raise MonitorError("Hermes's scorer printed something that isn't JSON") from exc
    return answer if isinstance(answer, list) else []


def score_all(items: list[dict[str, Any]], scorer: Scorer) -> dict[str, tuple[int, str]]:
    """Every item's score and reason, by ID. An item without a readable whole-number score is an
    error: Hermes's scorer drops it without a word, which would lose an important email."""

    scores: dict[str, tuple[int, str]] = {}
    for start in range(0, len(items), BATCH):
        for entry in scorer(items[start:start + BATCH]):
            if not isinstance(entry, dict):
                continue
            score = entry.get("score")
            if isinstance(score, bool) or not isinstance(score, int):
                continue
            reason = entry.get("reason")
            scores[str(entry.get("id"))] = (score, reason if isinstance(reason, str) else "")
    unscored = [item["id"] for item in items if item["id"] not in scores]
    if unscored:
        raise MonitorError(
            f"Hermes's scorer gave no readable score for {len(unscored)} of {len(items)} emails; "
            "none of them was marked seen, so the next run tries again"
        )
    return scores


# ---------------------------------------------------------------------------- state and output


def load_state(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MonitorError(f"Can't read the monitor's state at {path}; move it aside to start over") from exc
    if not isinstance(state, dict) or not isinstance(state.get("cursor"), (int, float)):
        raise MonitorError(f"The monitor's state at {path} isn't one it wrote; move it aside to start over")
    seen = state.get("seen")
    state["seen"] = {str(k): float(v) for k, v in seen.items()} if isinstance(seen, dict) else {}
    return state


def save_state(path: Path, state: dict[str, Any], *, file_mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    with os.fdopen(os.open(temporary, flags, file_mode), "w", encoding="utf-8") as handle:
        os.chmod(temporary, file_mode)
        json.dump(state, handle, indent=1, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, path)


def log_flagged(path: Path, items: list[dict[str, Any]], scores: dict[str, tuple[int, str]],
                flagged_at: float) -> None:
    """Append the existing scorer's important mail and prune after three days.

    Atomic replacement prevents the host reader seeing a half-written file.
    Retries keep the first flag time per ID; no body, snippet or login is logged.
    A failed log write fails the run before its cursor moves, like state failure.
    """

    entries: list[FlaggedMail] = []
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("emails"), list):
                raise ValueError("invalid log")
            for entry in data["emails"]:
                if (not isinstance(entry, dict)
                        or any(not isinstance(entry.get(key), str) for key in
                               ("message_id", "sender", "subject", "reason", "link"))
                        or not entry["message_id"]
                        or isinstance(entry.get("score"), bool) or not isinstance(entry.get("score"), int)
                        or isinstance(entry.get("flagged_at"), bool)
                        or not isinstance(entry.get("flagged_at"), (int, float))):
                    raise ValueError("invalid entry")
                if entry["flagged_at"] >= flagged_at - FLAGGED_SECONDS:
                    entries.append(entry)
        except (OSError, ValueError) as exc:
            raise MonitorError("Can't read the flagged-mail log; repair it before the next monitor run") from exc
    known = {entry["message_id"] for entry in entries}
    for item in items:
        score, reason = scores[item["id"]]
        if score >= THRESHOLD and item["id"] not in known:
            entries.append(FlaggedMail(message_id=item["id"], sender=item["from"], subject=item["subject"],
                score=score, reason=reason, link=item["url"], flagged_at=flagged_at))
            known.add(item["id"])
    # An install may give this directory a setgid review group. Only the log is
    # group-readable, including every atomic replacement; state stays 0600.
    save_state(path, {"version": 1, "emails": entries}, file_mode=0o640)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def ping(item: dict[str, Any], score: int, reason: str) -> str:
    """One email's message. Short enough that a swipe-reply quotes all of it back to Diablo."""

    lines = [
        f"Important email ({score}/10)",
        f"From: {_clip(item['from'], 80)}",
        f"Subject: {_clip(item['subject'], 120)}",
    ]
    if reason:
        lines.append(f"Why: {_clip(reason, 120)}")
    lines.append(item["url"])
    return "\n".join(lines)[:PING_CHARS]


RECOVERED = "Mail monitor is working again."
QUIET_AFTER = "I'll stay quiet about this until it's fixed, and say when it's working again."
FIRST_RUN = "Mail monitor is on. I'll message you here when an important email arrives. I can read mail, not send it."
FOOTER = "Swipe-reply to a ping and I'll draft an answer here; I can't send email."


# ---------------------------------------------------------------------------- one run


def run(home: Path, *, http: Http = http_json, scorer: Scorer = hermes_scorer,
        now: Callable[[], float] = time.time,
        deliver: Callable[[str], str] | None = None) -> str:
    """One monitor run; returns what Hermes should send (empty for nothing)."""

    started = now()
    state_path = home / "mail-monitor" / "state.json"
    login = load_login(token_path(home))
    token = access_token(login, http)
    state = load_state(state_path)
    if state is None:
        # First run: start from now rather than pinging about the whole inbox.
        log_flagged(state_path.with_name(FLAGGED_LOG_NAME), [], {}, started)
        output = deliver(FIRST_RUN) if deliver else FIRST_RUN
        save_state(state_path, {"cursor": started, "seen": {}})
        return output

    seen: dict[str, float] = state["seen"]
    listed = new_message_ids(token, int(state["cursor"]) - OVERLAP_SECONDS, http)
    fresh = [message_id for message_id in listed if message_id not in seen]
    batch = fresh[:MAX_PER_RUN]
    items = [message_item(message_id, token, http) for message_id in batch]
    scores = score_all(items, scorer) if items else {}

    pings = [ping(item, *scores[item["id"]]) for item in items if scores[item["id"]][0] >= THRESHOLD]
    log_flagged(state_path.with_name(FLAGGED_LOG_NAME), items, scores, started)
    for message_id in batch:
        seen[message_id] = started
    # Only a run that got through everything moves the window on; otherwise the rest wait their turn.
    cursor = started if len(fresh) <= MAX_PER_RUN else state["cursor"]
    kept = {k: v for k, v in seen.items() if v >= started - SEEN_SECONDS}
    output = "\n\n".join(pings + [FOOTER]) if pings else ""
    # A confirmed Discord send, or the unchanged Telegram stdout handoff, comes
    # before advancing the cursor. A raised delivery error leaves it untouched.
    if output and deliver:
        output = deliver(output)
    save_state(state_path, {"cursor": cursor, "seen": kept})
    return output


# ---------------------------------------------------------------------------- optional Discord delivery

DELIVERY_FILE = "delivery.json"
SEND_TIMEOUT = 30.0
DISCORD_FOOTER = "Reply to a ping and I'll draft an answer here; I can't send email."
TELEGRAM_FALLBACK = (
    "I couldn't confirm Discord delivery, so this is coming through Telegram. "
    "You may also see it in Discord."
)


def delivery_route(home: Path) -> str | None:
    """Read only the installer's non-secret projection of the canonical gates.

    No file means the existing Telegram behavior. Invalid configuration fails
    before mail is read or its cursor advances; it never silently enables a route.
    """
    path = home / "mail-monitor" / DELIVERY_FILE
    if not path.exists():
        return None
    try:
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate field")
                result[key] = value
            return result
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique)
        if (not isinstance(data, dict)
                or set(data) != {"version", "enabled", "mail_pings", "home_channel_id"}
                or type(data["version"]) is not int or data["version"] != 1
                or type(data["enabled"]) is not bool or type(data["mail_pings"]) is not bool
                or not isinstance(data["home_channel_id"], str)
                or data["mail_pings"] and not data["enabled"]):
            raise ValueError("invalid route")
        if not (data["enabled"] and data["mail_pings"]):
            return None
        if not re.fullmatch(r"[0-9]{1,20}", data["home_channel_id"]):
            raise ValueError("invalid target")
        return "discord:" + data["home_channel_id"]
    except (OSError, UnicodeError, ValueError):
        raise MonitorError("Mail delivery settings need checking; no mail was advanced",
                           cause="mail delivery settings") from None


def route_ping(output: str, target: str | None, *, home: Path | None = None, runner=None) -> str:
    """Confirmed native Discord send => no cron output; otherwise Telegram fallback.

    The existing Hermes cron remains set to Telegram. Payload travels over stdin;
    captured CLI output (which may contain private details) is never reflected.
    No retry or agent run is started here. An uncertain send may appear twice.
    """
    if not target or not output:
        return output
    if not re.fullmatch(r"discord:[0-9]{1,20}", target):
        raise MonitorError("Mail delivery target needs checking", cause="mail delivery settings")
    text = output.replace(FOOTER, DISCORD_FOOTER)
    # Email metadata and scorer wording are untrusted: never ping server members.
    text = re.sub(r"@(everyone|here)\b", "@\u200b\\1", text, flags=re.IGNORECASE)
    text = text.replace("<@", "<@\u200b")
    try:
        done = (runner or subprocess.run)(
            ["hermes", "send", "--to", target, "--json", "--file", "-"],
            input=text, capture_output=True, text=True, encoding="utf-8",
            timeout=SEND_TIMEOUT, check=False,
            env={**os.environ, "HERMES_HOME": str((home or hermes_home()).resolve())},
        )
        answer = json.loads(done.stdout)
        if done.returncode == 0 and isinstance(answer, dict) and answer.get("success") is True:
            return ""
    except (OSError, subprocess.TimeoutExpired, UnicodeError, ValueError):
        pass  # Explicit fallback is the result, not a swallowed delivery failure.
    return TELEGRAM_FALLBACK + "\n\n" + output


def check(home: Path, *, http: Http = http_json) -> str:
    """For the install: the login works and is read-only, the inbox answers and Hermes's scorer is
    importable. Saves nothing, scores nothing, sends nothing."""

    token = access_token(load_login(token_path(home)), http)
    body = http(f"{API_BASE}/messages?{urllib.parse.urlencode({'q': 'in:inbox newer_than:1d', 'maxResults': '100'})}",
                None, token)
    count = len(body.get("messages") or [])
    try:
        import importlib.util

        found = importlib.util.find_spec("cron.scripts.classify_items") is not None
    except ModuleNotFoundError:
        found = False
    if not found:
        raise MonitorError("Hermes's scorer (cron.scripts.classify_items) isn't importable from this Python")
    more = "+" if body.get("nextPageToken") else ""
    return (
        f"Mail monitor check: the Gmail login works and is read-only; {count}{more} inbox emails in the last day; "
        "Hermes's scorer is there. Nothing was saved or sent."
    )


# ---------------------------------------------------------------------------- alert once


def failure_path(home: Path) -> Path:
    return home / "mail-monitor" / "failure.json"


def report_failure(home: Path, exc: BaseException, now: Callable[[], float] = time.time) -> int:
    """Alert on a new kind of failure (exit 1: Hermes delivers stderr); stay quiet on a repeat (exit 0,
    nothing printed). An unreadable or unwritable failure file only ever errs towards alerting."""

    if isinstance(exc, MonitorError):
        message, cause = str(exc), exc.cause
    else:  # a bug, not a known failure: its type is the cause, and the alert carries the trace
        message = f"unexpected {type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"
        cause = f"unexpected {type(exc).__name__}"
    path = failure_path(home)
    try:
        previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    except (OSError, ValueError):
        previous = None
    if isinstance(previous, dict) and previous.get("cause") == cause:
        previous["repeats"] = int(previous.get("repeats") or 0) + 1
        previous["last"] = now()
        try:
            save_state(path, previous)
        except OSError:
            pass  # only the repeat count is lost; this cause was already alerted
        return 0
    try:
        save_state(path, {"cause": cause, "message": message, "since": now(), "last": now(), "repeats": 0})
    except OSError as error:
        message += f"\n(Couldn't record this failure, so the next run alerts again: {error})"
    print(f"Mail monitor: {message}\n{QUIET_AFTER}", file=sys.stderr)
    return 1


def clear_failure(home: Path) -> bool:
    """After a good run: forget the failure; True when there was one, so Nic hears it's fixed."""

    path = failure_path(home)
    if not path.exists():
        return False
    path.unlink()
    return True


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    home = hermes_home()
    if args == ["--check"]:
        try:
            delivery_route(home)
            print(check(home))
        except MonitorError as exc:
            print(f"Mail monitor: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        target = delivery_route(home)
        output = run(home, deliver=lambda text: route_ping(text, target, home=home)) if target else run(home)
    except Exception as exc:  # noqa: BLE001 - every failure is reported, once per cause
        return report_failure(home, exc)
    try:
        recovered = clear_failure(home)
    except OSError:
        recovered = False  # the pings still go out; the next good run says it's working again
    recovery = RECOVERED if recovered else ""
    if recovery and target and not output:
        recovery = route_ping(recovery, target, home=home)
    parts = ([recovery] if recovery else []) + ([output] if output else [])
    if parts:
        print("\n\n".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
