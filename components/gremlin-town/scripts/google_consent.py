"""Nic's one-time Google consent (step D, ROA-18; Diablo's mail, ROA-91); never prints credential material.

Runs on Nic's PC with any Python 3.12+ and nothing else installed. It takes the OAuth client
file Google Cloud gives for a "Desktop app" client, opens Google's consent screen for exactly
the scopes its target accepts, and refuses anything else before a file is written.

- ``--for hub`` (the default): the two scopes the hub accepts (`sources/google/auth.py`'s
  `ALLOWED_SCOPES`), written as the credential file `GoogleCredential.load` reads: client_id,
  client_secret, refresh_token, account and scopes. Default output
  ~/.secrets/gremlin-town/google-credential.json. Installing it is docs/operations.md,
  "Switching on step D".
- ``--for diablo``: read-only Gmail alone by default; ``--gmail-edit`` requests only
  gmail.modify (which also permits sending). Written as the authorized-user file Hermes's
  `google-workspace` skill reads (`$HERMES_HOME/google_token.json`).
  Default output ~/.secrets/gremlin-town/diablo-google-token.json. Installing it is
  ops/audits/2026-09-29-mail-monitor-01-install.md.
- ``--for diablo-mail``: the mail monitor's separate read-only login, installed as
  `$HERMES_HOME/google_token_mail.json`. Default output
  ~/.secrets/gremlin-town/diablo-google-token-mail.json.

    python google_consent.py path/to/client_secret_....json [--for diablo]
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, NamedTuple

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
CALENDAR_CHECK_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events?maxResults=1"
CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_READ_ONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
# The same pair `sources/google/auth.py` enforces; a test keeps the two in step.
SCOPES = (CALENDAR_EVENTS_SCOPE, GMAIL_READ_ONLY_SCOPE)
SCOPE_WORDS = {
    CALENDAR_EVENTS_SCOPE: "See and change events on your calendars",
    GMAIL_READ_ONLY_SCOPE: "Read your email",
    GMAIL_MODIFY_SCOPE: "Read, compose, and send emails from your Gmail account",
}
DEFAULT_OUT = Path.home() / ".secrets" / "gremlin-town" / "google-credential.json"
# Diablo reads mail and nothing else: Google itself refuses any send, change or delete (ROA-91).
DIABLO_SCOPES = (GMAIL_READ_ONLY_SCOPE,)
DIABLO_OUT = Path.home() / ".secrets" / "gremlin-town" / "diablo-google-token.json"
WAIT_SECONDS = 300
TIMEOUT = 20.0


class ConsentError(RuntimeError):
    """A step failed; the message says what to do and never carries a secret."""


class Target(NamedTuple):
    """Who the credential is for: the scopes asked for, where it goes, and what is read to check it."""

    name: str
    scopes: tuple[str, ...]
    default_out: Path
    checks: tuple[str, ...]
    done: str
    next_step: str


HUB = Target(
    "hub", SCOPES, DEFAULT_OUT, (CALENDAR_CHECK_URL,), "calendar and email both answered",
    "Next: the hub install in docs/operations.md, 'Switching on step D', which needs Nic's go.",
)
DIABLO = Target(
    "diablo", DIABLO_SCOPES, DIABLO_OUT, (), "email answered, read-only",
    "Next: Diablo's install in ops/audits/2026-09-29-mail-monitor-01-install.md, which needs Nic's go.",
)
DIABLO_EDIT = Target(
    "diablo", (GMAIL_MODIFY_SCOPE,), DIABLO_OUT, (), "email answered, edit access (login can send)",
    "Next: apply the send block in docs/operations.md, 'Diablo Gmail edit access', before using the skill.",
)
DIABLO_MAIL = Target(
    "diablo-mail", DIABLO_SCOPES,
    Path.home() / ".secrets" / "gremlin-town" / "diablo-google-token-mail.json", (),
    "email answered, monitor login read-only",
    "Next: install google_token_mail.json and check the pings before changing Diablo's login; "
    "see docs/operations.md, 'Diablo Gmail edit access'.",
)
TARGETS = {target.name: target for target in (HUB, DIABLO, DIABLO_MAIL)}


def load_client(path: Path) -> dict[str, str]:
    """The client ID and secret from Google Cloud's downloaded client file ("Desktop app" only)."""

    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ConsentError(f"Can't read the client file {path}: {exc.strerror}") from exc
    except json.JSONDecodeError as exc:
        raise ConsentError(f"The client file {path} isn't valid JSON") from exc
    if not isinstance(data, dict):
        raise ConsentError("The client file isn't the one Google Cloud downloads for an OAuth client")
    if "web" in data:
        raise ConsentError(
            'This client is a "Web application". Create an OAuth client of type "Desktop app" instead'
        )
    installed = data.get("installed")
    if not isinstance(installed, dict):
        raise ConsentError('The client file has no "installed" section; download it from a "Desktop app" client')
    client = {field: installed.get(field) for field in ("client_id", "client_secret")}
    for field, value in client.items():
        if not isinstance(value, str) or not value:
            raise ConsentError(f"The client file is missing {field}")
    return client  # type: ignore[return-value]


def pkce_pair() -> tuple[str, str]:
    """A PKCE verifier and its S256 challenge."""

    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def consent_url(
    client_id: str, redirect_uri: str, state: str, challenge: str, scopes: tuple[str, ...] = SCOPES
) -> str:
    """Google's consent screen for exactly ``scopes``, asking for a refresh token."""

    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "offline",
        "prompt": "consent",
    })


def scope_problem(granted: str, scopes: tuple[str, ...] = SCOPES) -> str | None:
    """Why the granted scopes aren't exactly ``scopes``, in words, or ``None`` when they are.
    Google lets you untick any box, and adds nothing unless asked."""

    have = set(granted.split())
    missing = [SCOPE_WORDS[scope] for scope in scopes if scope not in have]
    extra = sorted(have - set(scopes))
    if missing:
        boxes = "both boxes" if len(scopes) == 2 else "the box" if len(scopes) == 1 else "every box"
        return "Not granted: " + "; ".join(missing) + f". Run it again and tick {boxes}"
    if extra:
        return "Google granted more than asked for (" + ", ".join(extra) + "); it would be refused"
    return None


def credential_record(client: dict[str, str], tokens: dict[str, Any], account: str) -> dict[str, Any]:
    """The file `GoogleCredential.load` reads."""

    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ConsentError(
            "Google sent no refresh token. Remove the app's access at myaccount.google.com/permissions, "
            "then run this again"
        )
    problem = scope_problem(str(tokens.get("scope", "")))
    if problem is not None:
        raise ConsentError(problem)
    return {
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": refresh_token,
        "account": account,
        "scopes": list(SCOPES),
    }


def hermes_token_record(
    client: dict[str, str], tokens: dict[str, Any], account: str, now: datetime | None = None,
    *, scopes: tuple[str, ...] = DIABLO_SCOPES,
) -> dict[str, Any]:
    """The authorized-user file Hermes's `google-workspace` skill reads (google-auth's own format).
    It carries the access token and its expiry because Hermes refreshes only a token it can see
    has expired; Diablo's mail monitor reads just the client, the refresh token and the token URI."""

    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise ConsentError(
            "Google sent no refresh token. Remove the app's access at myaccount.google.com/permissions, "
            "then run this again"
        )
    if scopes not in (DIABLO_SCOPES, DIABLO_EDIT.scopes):
        raise ConsentError("Diablo accepts only gmail.readonly alone or gmail.modify alone")
    problem = scope_problem(str(tokens.get("scope", "")), scopes)
    if problem is not None:
        raise ConsentError(problem)
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ConsentError("Google's token answer had no access token")
    lifetime = tokens.get("expires_in")
    seconds = int(lifetime) if isinstance(lifetime, (int, float)) and not isinstance(lifetime, bool) else 0
    expiry = (now or datetime.now(UTC)) + timedelta(seconds=max(seconds - 60, 0))
    return {
        "type": "authorized_user",
        "token": access_token,
        "refresh_token": refresh_token,
        "token_uri": TOKEN_URL,
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "scopes": list(scopes),
        "account": account,
        "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def write_credential(path: Path, record: dict[str, Any], *, force: bool) -> None:
    """Write the credential file readable only by its owner; never over an existing one unless
    ``force``."""

    if path.exists() and not force:
        raise ConsentError(f"{path} already exists. Pass --force to replace it")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    with os.fdopen(os.open(path, flags, 0o600), "w", encoding="utf-8") as handle:
        json.dump(record, handle, indent=2)
        handle.write("\n")
    os.chmod(path, 0o600)


def _request(url: str, *, data: dict[str, str] | None = None, token: str | None = None) -> dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("ascii") if data is not None else None
    request = urllib.request.Request(url, data=body)
    if token is not None:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Google's error bodies carry a code and a description, never the secret sent.
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            detail = {}
        error = detail.get("error")
        if isinstance(error, dict):
            error = error.get("message") or error.get("status")
        raise ConsentError(f"Google answered {exc.code} for {url.split('?')[0]}: {error or exc.reason}") from exc


def _wait_for_code(server: http.server.HTTPServer, state: str) -> str:
    """Serve the loopback redirect until Google's answer arrives, or `WAIT_SECONDS` pass."""

    answer: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server's name
            query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(self.path).query))
            if "code" not in query and "error" not in query:
                self.send_response(404)
                self.end_headers()
                return
            answer.update(query)
            done = "code" in query and query.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"Done. You can close this tab and go back to the terminal." if done
                else b"That didn't work. Go back to the terminal to see why."
            )

        def log_message(self, *_args: Any) -> None:
            return  # the query holds the one-time code; keep it off the terminal

    server.RequestHandlerClass = Handler
    deadline = time.monotonic() + WAIT_SECONDS
    while not answer:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ConsentError(f"No answer from Google within {WAIT_SECONDS // 60} minutes")
        server.timeout = remaining
        server.handle_request()
    if "error" in answer:
        raise ConsentError(f"Google said: {answer['error']}")
    if answer.get("state") != state:
        raise ConsentError("The answer didn't match this run (state mismatch); run it again")
    return answer["code"]


def run(client_file: Path, out: Path, *, force: bool, open_browser: bool, target: Target = HUB) -> str:
    """The whole flow; returns the account the credential is for."""

    client = load_client(client_file)
    if out.exists() and not force:
        raise ConsentError(f"{out} already exists. Pass --force to replace it")
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
    try:
        redirect_uri = f"http://127.0.0.1:{server.server_port}/"
        url = consent_url(client["client_id"], redirect_uri, state, challenge, target.scopes)
        print("Opening Google's consent screen. If no browser opens, paste this link into one:\n")
        print(url, "\n")
        count = {1: "one thing", 2: "two things"}.get(len(target.scopes), f"{len(target.scopes)} things")
        print(f"Approve exactly {count}: " + " and ".join(f'"{SCOPE_WORDS[s]}"' for s in target.scopes) + ".")
        print("If Google asks for anything else, stop and close the tab.\n")
        if open_browser:
            webbrowser.open(url)
        code = _wait_for_code(server, state)
    finally:
        server.server_close()

    tokens = _request(TOKEN_URL, data={
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "code": code,
        "code_verifier": verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })
    access_token = tokens.get("access_token")
    if not isinstance(access_token, str) or not access_token:
        raise ConsentError("Google's token answer had no access token")
    problem = scope_problem(str(tokens.get("scope", "")), target.scopes)
    if problem is not None:
        raise ConsentError(problem)
    account = _request(PROFILE_URL, token=access_token).get("emailAddress")
    if not isinstance(account, str) or not account:
        raise ConsentError("Couldn't read which Google account this is")
    for check in target.checks:
        _request(check, token=access_token)
    if target.name in ("diablo", "diablo-mail"):
        record = hermes_token_record(client, tokens, account, scopes=target.scopes)
    else:
        record = credential_record(client, tokens, account)
    write_credential(out, record, force=force)
    return account


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create a Google credential for the hub, Diablo or the read-only mail monitor."
    )
    parser.add_argument("client_file", type=Path, help='the JSON Google Cloud downloads for a "Desktop app" client')
    parser.add_argument(
        "--for", dest="target", choices=sorted(TARGETS), default=HUB.name,
        help="hub: calendar and read-only mail (default); diablo: read-only mail unless --gmail-edit; "
             "diablo-mail: separate read-only monitor login",
    )
    parser.add_argument(
        "--gmail-edit", action="store_true",
        help="only with --for diablo: request gmail.modify alone; the login can also send",
    )
    parser.add_argument(
        "--out", type=Path, default=None,
        help="where to write it (default ~/.secrets/gremlin-town/google-credential.json for hub, "
             "diablo-google-token.json for diablo, diablo-google-token-mail.json for diablo-mail)",
    )
    parser.add_argument("--force", action="store_true", help="replace an existing credential file")
    parser.add_argument("--no-browser", action="store_true", help="print the link without opening a browser")
    args = parser.parse_args(argv)
    if args.gmail_edit and args.target != "diablo":
        parser.error("--gmail-edit requires --for diablo")
    target = TARGETS[args.target]
    if args.gmail_edit:
        target = DIABLO_EDIT
    out = args.out or target.default_out
    try:
        account = run(args.client_file, out, force=args.force, open_browser=not args.no_browser, target=target)
    except ConsentError as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    print(f"Done for {account}: {target.done}.")
    print(f"Credential written to {out} (nothing secret was printed).")
    print(target.next_step)
    return 0


if __name__ == "__main__":
    sys.exit(main())
