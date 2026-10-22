"""Hands each Telegram message to the courier as a file (step E spec §2).

Diablo's Hermes gives agents no way to send a message on their own, so the review and
daytime returns reach Nic's Telegram through a courier: `deploy/telegram-courier.sh`, run
as root after each review or moments run, posts every `<name>.txt` in this folder with
`hermes send` inside Diablo's container, then moves it to `sent/`. GT still creates the
request to Diablo first: it is the record he reads when Nic replies by number.

Nothing here runs unless `[review] telegram_spool_dir` is set. **Accepted — Nic, 29 Sep 2026.**
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from pathlib import Path

SPOOL_FILE_MODE = 0o640
SENT_FOLDER = "sent"  # where the courier moves each file once Telegram has it
# A client_operation_id becomes a file name: no separators, no leading dot (the courier
# skips hidden files, and this module's own temporary files are hidden).
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,199}")


class SpoolError(RuntimeError):
    """The courier's folder is unusable, or a message could not be written to it."""


def was_sent(spool_dir: str, client_operation_id: str) -> bool:
    """Only the courier's sent file proves delivery; never read its ledger."""
    if not _SAFE_NAME.fullmatch(client_operation_id):
        raise SpoolError(f"{client_operation_id!r} is not a safe spool file name")
    target = require_folder(spool_dir) / SENT_FOLDER / f"{client_operation_id}.txt"
    return target.is_file() and not target.is_symlink()


def require_folder(spool_dir: str) -> Path:
    """The spool folder, checked before anything is logged or sent; `SpoolError` if unusable."""

    folder = Path(spool_dir)
    if not folder.is_dir():
        raise SpoolError(f"the Telegram spool folder {folder} does not exist")
    if not os.access(folder, os.W_OK | os.X_OK):
        raise SpoolError(f"the Telegram spool folder {folder} is not writable")
    return folder


def write_message(spool_dir: str, client_operation_id: str, text: str) -> Path | None:
    """Write `text`, exactly, to `<spool_dir>/<client_operation_id>.txt` for the courier.

    Atomic: a hidden temporary file, then a hard link, which never replaces an existing
    file. Mode 0640. A replay writes nothing and returns `None`: that message is still
    waiting here, or is already in `sent/`. Any failure raises `SpoolError`.
    """

    if not _SAFE_NAME.fullmatch(client_operation_id):
        raise SpoolError(f"{client_operation_id!r} is not a safe spool file name")
    folder = require_folder(spool_dir)
    target = folder / f"{client_operation_id}.txt"
    if os.path.lexists(target) or os.path.lexists(folder / SENT_FOLDER / target.name):
        return None
    failure = f"could not write {target.name} to the Telegram spool folder {folder}"
    try:
        handle, temporary = tempfile.mkstemp(prefix=f".{client_operation_id}.", suffix=".tmp", dir=folder)
    except OSError as exc:
        raise SpoolError(failure) from exc
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, SPOOL_FILE_MODE)
        os.link(temporary, target)
    except FileExistsError:
        return None  # a concurrent replay linked the same message first
    except OSError as exc:
        raise SpoolError(failure) from exc
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)
    return target
