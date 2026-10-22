"""Prepare/apply one checked E23 config change. Never restart, send, or recover.

Uses the existing Hermes profile. Plans contain hashes, never source contents or
channel identifiers. Config is read again at apply; every input must still match.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import tomllib


class InstallError(RuntimeError):
    """Only fixed, non-secret messages reach the operator."""


ACTIONS = {"mail-pings": "mail-monitor/delivery.json", "home-channel": ".env",
           "wake-route": "webhook_subscriptions.json"}


def digest(blob: bytes | None) -> str | None:
    return hashlib.sha256(blob).hexdigest() if blob is not None else None


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InstallError("Duplicate configuration field; stop and inspect locally")
        result[key] = value
    return result


def json_bytes(value) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def gates(raw: bytes) -> dict:
    try:
        table = tomllib.loads(raw.decode("utf-8")).get("discord", {})
        if not isinstance(table, dict):
            raise ValueError
        result = {key: table.get(key, False) for key in ("enabled", "mail_pings", "diablo_home")}
        if any(type(value) is not bool for value in result.values()):
            raise ValueError
        if not result["enabled"] and (result["mail_pings"] or result["diablo_home"]):
            raise ValueError
        result["home_channel_id"] = table.get("home_channel_id", "")
        if not isinstance(result["home_channel_id"], str):
            raise ValueError
        if ((result["mail_pings"] or result["diablo_home"])
                and not re.fullmatch(r"[0-9]{1,20}", result["home_channel_id"])):
            raise ValueError
        return result
    except (ValueError, UnicodeError):
        raise InstallError("Canonical Discord settings are invalid; stop and inspect locally") from None


def single_line_environment(text: str) -> None:
    """Validate the narrow syntax this byte-preserving edit understands.

    Native dotenv permits quoted multiline values. Refuse them, including in an
    unrelated setting, rather than mistake their physical lines for assignments.
    No source value or unfamiliar line is included in the refusal.
    """
    failure = "Environment syntax needs checking; multiline or unfamiliar settings were not changed"
    if "\x00" in text:
        raise InstallError(failure)
    assignment = re.compile(
        r"^[ \t]*(?:export[ \t]+)?(?:[A-Za-z_][A-Za-z0-9_]*|'[^']+'|\"[^\"]+\")"
        r"[ \t]*=[ \t]*(.*)$")
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        match = assignment.fullmatch(line)
        if not match:
            raise InstallError(failure)
        value = match[1]
        if not value or value[0] not in "\"'":
            continue
        quote, offset = value[0], 1
        while offset < len(value):
            if value[offset] == "\\":
                offset += 2
                continue
            if value[offset] == quote:
                remainder = value[offset + 1:].lstrip()
                if remainder and not remainder.startswith("#"):
                    raise InstallError(failure)
                break
            offset += 1
        else:
            raise InstallError(failure)


def build(action: str, current: bytes | None, settings: dict) -> bytes:
    if action == "mail-pings":
        enabled = settings["enabled"] and settings["mail_pings"]
        if current is not None:
            old = json.loads(current, object_pairs_hook=unique)
            if (not isinstance(old, dict)
                    or set(old) != {"version", "enabled", "mail_pings", "home_channel_id"}
                    or type(old["version"]) is not int or old["version"] != 1
                    or any(type(old[key]) is not bool for key in ("enabled", "mail_pings"))
                    or not isinstance(old["home_channel_id"], str)):
                raise InstallError("Existing mail route is unfamiliar; stop and inspect locally")
        return json_bytes({"version": 1, "enabled": enabled, "mail_pings": enabled,
                           "home_channel_id": settings["home_channel_id"] if enabled else ""})
    if current is None:
        raise InstallError("Existing Hermes configuration is missing; stop and inspect locally")
    if action == "home-channel":
        if not settings["diablo_home"]:
            return current  # Retain configured Discord and Telegram; do not invent a rollback.
        text = current.decode("utf-8")
        single_line_environment(text)
        pattern = (r"(?m)^[ \t]*(?:export[ \t]+)?"
                   r"(?:DISCORD_HOME_CHANNEL|'DISCORD_HOME_CHANNEL'|\"DISCORD_HOME_CHANNEL\")"
                   r"[ \t]*=[^\r\n]*")
        if len(re.findall(pattern, text)) > 1:
            raise InstallError("Duplicate Discord home setting; stop and inspect locally")
        replacement = "DISCORD_HOME_CHANNEL=" + settings["home_channel_id"]
        if re.search(pattern, text):
            text = re.sub(pattern, lambda _: replacement, text)
        else:
            newline = "\r\n" if "\r\n" in text else "\n"
            text += ("" if not text or text.endswith("\n") else newline) + replacement + newline
        return text.encode("utf-8")
    if action == "wake-route":
        routes = json.loads(current, object_pairs_hook=unique)
        route = routes.get("gremlin-town-wake") if isinstance(routes, dict) else None
        if (not isinstance(route, dict) or route.get("enabled") is not True
                or route.get("events") != ["gt.request.ready"]
                or route.get("deliver") not in ("telegram", "discord")
                or not isinstance(route.get("secret"), str) or not route["secret"]
                or not isinstance(route.get("prompt"), str) or not route["prompt"]
                or route.get("skills") != ["gremlin-town"]
                or route.get("toolsets") != ["hermes-telegram"]
                or route.get("deliver_extra")):
            raise InstallError("Existing wake route is unfamiliar; stop and inspect locally")
        # Only the native final-response destination changes. No added tool text.
        route["deliver"] = "discord" if settings["diablo_home"] else "telegram"
        return json_bytes(routes)
    raise InstallError("Unknown install action")


def read_regular(path: Path, *, optional: bool = False) -> bytes | None:
    if path.is_symlink():
        raise InstallError("A configuration path is a link; stop and inspect locally")
    if not path.exists() and optional:
        return None
    if not path.is_file():
        raise InstallError("A required configuration file is missing or not regular")
    return path.read_bytes()


def target_path(home: Path, action: str) -> Path:
    if action not in ACTIONS or not home.is_dir() or home.is_symlink():
        raise InstallError("Existing Hermes home needs checking")
    target = home / ACTIONS[action]
    if target.parent != home and target.parent.exists() and (
            target.parent.is_symlink() or not target.parent.is_dir()):
        raise InstallError("Mail monitor directory needs checking")
    if not target.resolve().is_relative_to(home.resolve()):
        raise InstallError("Configuration target leaves the existing Hermes home")
    return target


def snapshot(home: Path, config: Path, action: str):
    target = target_path(home, action)
    source = read_regular(config)
    before = read_regular(target, optional=action == "mail-pings")
    after = build(action, before, gates(source))
    # Bind paths without persisting potentially private path names or contents.
    plan = {"version": 1, "action": action,
            "home": digest(str(home.resolve()).encode()),
            "config": digest(str(config.resolve()).encode()),
            "source": digest(source), "before": digest(before), "after": digest(after),
            "changed": before != after}
    return plan, target, before, after


def exclusive_file(path: Path, blob: bytes, mode: int = 0o600):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    with os.fdopen(fd, "wb") as handle:
        handle.write(blob)
        handle.flush()
        os.fsync(handle.fileno())


def prepare(home: Path, config: Path, action: str, plan_file: Path) -> bool:
    plan, _, _, _ = snapshot(home, config, action)
    exclusive_file(plan_file, json_bytes(plan), 0o400)
    return plan["changed"]


def apply(home: Path, config: Path, action: str, plan_file: Path) -> bool:
    expected = json.loads(read_regular(plan_file), object_pairs_hook=unique)
    actual, target, before, after = snapshot(home, config, action)
    if expected != actual:
        raise InstallError("Configuration changed since preparation; nothing was applied")
    if not actual["changed"]:
        return False
    # Backup first, including an explicit absent marker for the new projection.
    # Any failure leaves its artifacts for inspection; there is no retry/rollback.
    backup_root = home / "discord-home-backups"
    if backup_root.exists() and (backup_root.is_symlink() or not backup_root.is_dir()):
        raise InstallError("Backup directory needs checking")
    backup_root.mkdir(mode=0o700, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix=action + "-", dir=backup_root))
    os.chmod(backup, 0o700)
    exclusive_file(backup / "before", before if before is not None else b"")
    exclusive_file(backup / "plan.json", json_bytes(actual))
    if read_regular(backup / "before") != (before if before is not None else b""):
        raise InstallError("Backup check failed; nothing was applied")
    # Both files are checked after backup and immediately before publication.
    fresh, _, _, _ = snapshot(home, config, action)
    if fresh != actual:
        raise InstallError("Configuration changed during backup; nothing was applied")
    if not target.parent.exists():
        target.parent.mkdir(mode=0o700)
        if hasattr(os, "chown"):
            owner = home.stat()
            os.chown(target.parent, owner.st_uid, owner.st_gid)
    metadata = target.stat() if before is not None else None
    fd, name = tempfile.mkstemp(prefix=".discord-home-", dir=target.parent)
    temporary = Path(name)
    with os.fdopen(fd, "wb") as handle:
        handle.write(after)
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temporary, stat.S_IMODE(metadata.st_mode) if metadata else 0o600)
    if hasattr(os, "chown"):
        owner = metadata or home.stat()
        os.chown(temporary, owner.st_uid, owner.st_gid)
    final, _, _, _ = snapshot(home, config, action)
    if final != actual:
        raise InstallError("Configuration changed before replacement; nothing was applied")
    os.replace(temporary, target)
    if read_regular(target) != after:
        raise InstallError("Installed file did not match; stop before any further step")
    exclusive_file(backup / "applied", b"checked\n")
    return True


def main(argv=None) -> int:
    class QuietParser(argparse.ArgumentParser):
        def error(self, message):
            self.exit(2, "Invalid installer arguments; use --help (values suppressed)\n")
    parser = QuietParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "apply"))
    parser.add_argument("action", choices=tuple(ACTIONS))
    parser.add_argument("--hermes-home", required=True, type=Path)
    parser.add_argument("--gt-config", required=True, type=Path)
    parser.add_argument("--plan", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        operation = prepare if args.mode == "prepare" else apply
        changed = operation(args.hermes_home, args.gt_config, args.action, args.plan)
    except InstallError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except Exception:
        # OS, JSON, encoding and permission errors can contain secrets or IDs.
        print("Install step failed; stop and inspect protected local files", file=sys.stderr)
        return 1
    print(("Change prepared; no live configuration changed" if args.mode == "prepare"
           else "One file applied and checked; no service restarted") if changed
          else "No configuration change needed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
