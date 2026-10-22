#!/usr/bin/env python3
"""Block/restore send and reply in one pinned Hermes google_api.py; standard library only.

Run as root on the hub with --skill-file pointing into Diablo's data mount.
This is a skill-side block, not a guarantee: a gmail.modify login can still send.
No credentials are read and no network, container or service command is run.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path

KNOWN_SHA256 = "d3822ef5cbdfad1d280fb619b9d85f2bdc0f8d8756398eb0189a126e8a6fbf89"
BACKUP_SUFFIX = ".gmail-send-original"
REFUSAL = "Gmail send and reply are OFF in Diablo's skill. Ask Nic to run gmail_send_toggle.py on."


class ToggleError(RuntimeError):
    """Refuse rather than patch or restore an unknown file."""


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def blocked_bytes(original: bytes) -> bytes:
    if digest(original) != KNOWN_SHA256:
        raise ToggleError("Unknown google_api.py version; cannot patch it. Review and pin the new version first.")
    source = original.decode("utf-8")
    for name in ("gmail_send", "gmail_reply"):
        anchor = f"def {name}(args):\n"
        if source.count(anchor) != 1:
            raise ToggleError(f"Pinned file has no unique {name} function")
        source = source.replace(anchor, anchor + f"    raise SystemExit({REFUSAL!r})\n", 1)
    start = source.index('    p = gmail_sub.add_parser("send")\n')
    end = source.index('    p = gmail_sub.add_parser("labels")\n', start)
    source = source[:start] + source[end:]
    source = "\n".join(
        line for line in source.split("\n")
        if not line.startswith(("  python google_api.py gmail send ", "  python google_api.py gmail reply "))
    )
    compile(source, "google_api.py", "exec")
    return source.encode("utf-8")


def read_regular(path: Path) -> bytes:
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ToggleError(f"Expected a regular file, not a symlink: {path}")
    return path.read_bytes()


def inspect(path: Path) -> tuple[str, bytes, bytes | None]:
    current = read_regular(path)
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    original = None
    if backup.exists() or backup.is_symlink():
        original = read_regular(backup)
        if digest(original) != KNOWN_SHA256:
            raise ToggleError(f"Backup is not the known original: {backup}; refusing to change anything")
    if digest(current) == KNOWN_SHA256:
        return "on", current, original
    if original is not None and current == blocked_bytes(original):
        return "off", current, original
    raise ToggleError(
        f"Unknown google_api.py version (sha256 {digest(current)}), or missing original backup; "
        "refusing to change anything. Review and pin the version before re-applying the block."
    )


def replace_file(path: Path, before: bytes, after: bytes) -> None:
    metadata = path.stat()
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temporary)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(after)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_path, stat.S_IMODE(metadata.st_mode))
        if hasattr(os, "chown"):
            os.chown(temp_path, metadata.st_uid, metadata.st_gid)
        if read_regular(path) != before:
            raise ToggleError("google_api.py changed during the toggle; refusing to overwrite it")
        os.replace(temp_path, path)
    finally:
        if temp_path.exists():
            temp_path.unlink()


def toggle(action: str, path: Path) -> str:
    if action not in ("status", "off", "on"):
        raise ToggleError(f"Unknown action: {action}")
    if action == "status":
        return inspect(path)[0]
    lock = path.with_name(path.name + ".gmail-send-lock")
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise ToggleError(f"Toggle lock exists: {lock}; check whether another toggle is running") from exc
    try:
        os.close(fd)
        state, current, original = inspect(path)
        if state == action:
            return state
        if action == "off":
            replacement = blocked_bytes(current)
            backup = path.with_name(path.name + BACKUP_SUFFIX)
            if original is None:
                fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(current)
                    handle.flush()
                    os.fsync(handle.fileno())
                if read_regular(backup) != current:
                    raise ToggleError("Original backup failed verification; file was not patched")
        else:
            if original is None:
                raise ToggleError("Cannot restore send without the original backup")
            replacement = original
        replace_file(path, current, replacement)
        result = inspect(path)[0]
        if result != action:
            raise ToggleError("Toggle did not reach the requested state")
        return result
    finally:
        lock.unlink()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "off", "on"))
    parser.add_argument("--skill-file", type=Path, required=True, help="host path to Diablo's google_api.py")
    args = parser.parse_args(argv)
    try:
        state = toggle(args.action, args.skill_file)
    except (ToggleError, OSError) as exc:
        print(f"Gmail send toggle: {exc}", file=sys.stderr)
        return 1
    print(f"Gmail send/reply: {state.upper()} ({args.skill_file})")
    print("Skill-side block only; a gmail.modify login itself can send.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
