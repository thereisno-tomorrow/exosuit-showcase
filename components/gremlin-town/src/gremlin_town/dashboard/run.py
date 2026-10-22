"""Hub-only setup and listener commands; never deliver or print credentials."""
import argparse
from dataclasses import replace
import os
from pathlib import Path
import secrets
import sqlite3
import sys
import tomllib

import anyio
import uvicorn

from .app import create_app
from .auth import AuthService
from .settings import DashboardSettings


def load_settings(path: Path) -> DashboardSettings:
    with path.open("rb") as source:
        config = tomllib.load(source)
    settings = DashboardSettings.from_table(config.get("dashboard"), base=path.resolve().parent)
    credential = os.environ.get("GT_DASHBOARD_SESSION_SECRET_FILE")
    return replace(settings, session_secret_file=Path(credential)) if credential else settings


def _exclusive_file(path: Path):
    # Existing files and symlinks fail. Never replace an unknown credential/link.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    return os.fdopen(descriptor, "wb")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="gremlin-town dashboard")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "enrol", "revoke-all"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name == "enrol":
            command.add_argument("--output", type=Path, required=True)
    secret = commands.add_parser("init-secret")
    secret.add_argument("--output", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return int(error.code) if isinstance(error.code, int) else 2
    try:
        if args.command == "init-secret":
            with _exclusive_file(args.output) as target:
                target.write(secrets.token_bytes(64))
            sys.stdout.write("Dashboard session credential saved to the requested file.\n")
            return 0
        settings = load_settings(args.config)
        if not settings.enabled:
            sys.stderr.write("Dashboard is off. No command was run.\n")
            return 2
        if args.command == "serve":
            if not settings.reads:
                uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                            workers=1, access_log=False, proxy_headers=False)
                return 0
            from gremlin_town.runtime import build_runtime
            from .host import create_dashboard_app
            runtime = build_runtime(args.config, start_worker=False)
            brain_close_attempted = False
            brain_close_failed = False
            async def close_brain():
                nonlocal brain_close_attempted, brain_close_failed
                if brain_close_attempted or runtime.brain_runtime is None:
                    return
                brain_close_attempted = True
                try:
                    await runtime.brain_runtime.aclose()
                except Exception:
                    # Report a failed exit below, without a private provider
                    # exception reaching Uvicorn's shutdown traceback logger.
                    brain_close_failed = True
            try:
                app = create_dashboard_app(settings, runtime)
                # Close async providers while their serving loop still exists.
                app.router.add_event_handler("shutdown", close_brain)
                uvicorn.run(app, host=settings.host, port=settings.port,
                            workers=1, access_log=False, proxy_headers=False)
            finally:
                try:
                    # Also covers failure before the ASGI lifespan starts.
                    anyio.run(close_brain)
                finally:
                    runtime.close()
                if brain_close_failed:
                    raise RuntimeError("Dashboard provider cleanup failed") from None
            return 0
        auth = AuthService(settings)
        if args.command == "enrol":
            with _exclusive_file(args.output) as target:
                token = auth.issue_enrollment()
                target.write(f"{settings.origin}/enrol#token={token}\n".encode())
            sys.stdout.write("One-time enrolment link saved to the requested file; expires in 15 minutes.\n")
        else:
            auth.revoke_all()
            sys.stdout.write("Dashboard passkeys, sessions and outstanding enrolment links revoked.\n")
        return 0
    except Exception:
        # Runtime paths, TOML strings and library messages may hold private data.
        # Fail explicitly without reflecting any of them into logs or the CLI.
        sys.stderr.write("Dashboard command failed. Check the configuration, protected files and output-file availability.\n")
        return 2
