#!/usr/bin/env python3
"""Fail-closed work-backup validation; never restore authentication state."""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sqlite3
import tarfile
import tempfile
import tomllib
from contextlib import closing
from pathlib import Path

FILES = frozenset({"MANIFEST", "SHA256SUMS", "config.toml", "participants.toml", "gremlin-town.sqlite3"})
HASHED_FILES = FILES - {"SHA256SUMS"}
TEXT_LIMIT = 1024 * 1024
DATABASE_LIMIT = 8 * 1024**3


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def validate_config(config_bytes: bytes, participant_bytes: bytes) -> None:
    # Use the installed parser's allowlists; unknown fields must not silently
    # smuggle inline credentials into an otherwise non-secret configuration.
    from gremlin_town.runtime import (
        MCP_CLIENT_CONFIG_KEYS, PARTICIPANT_CONFIG_KEYS, ROUTE_CONFIG_KEYS, RUNTIME_CONFIG_SCHEMA,
    )

    config = tomllib.loads(config_bytes.decode("utf-8"))
    # The shared schema includes work-01's archive_superseded switch and the
    # accepted, unread [work] door_participant. Backups preserve both for work-02.
    require(set(config) <= set(RUNTIME_CONFIG_SCHEMA), "unknown backup configuration table")
    for name, table in config.items():
        require(isinstance(table, dict), "backup configuration must contain tables")
        require(set(table) <= RUNTIME_CONFIG_SCHEMA[name], "unknown or secret backup configuration field")
        require(all(not isinstance(value, dict) for value in table.values()), "nested backup configuration is forbidden")
        for field, value in table.items():
            # [[mcp.client]] is the only table array; check each entry like a table.
            if isinstance(value, list) and any(isinstance(item, dict) for item in value):
                require((name, field) == ("mcp", "client"), "nested backup configuration is forbidden")
                for entry in value:
                    require(isinstance(entry, dict) and set(entry) <= MCP_CLIENT_CONFIG_KEYS,
                            "unknown or secret backup configuration field")
                    require(all(not isinstance(item, dict) for item in entry.values()), "nested backup configuration is forbidden")
    participants = tomllib.loads(participant_bytes.decode("utf-8"))
    require(set(participants) <= {"participant", "route"}, "unknown participant configuration table")
    for name, allowed in (("participant", PARTICIPANT_CONFIG_KEYS), ("route", ROUTE_CONFIG_KEYS)):
        rows = participants.get(name, [])
        require(isinstance(rows, list), "participant configuration must contain table arrays")
        for row in rows:
            require(isinstance(row, dict) and set(row) <= allowed, "unknown or secret participant field")
            require(all(not isinstance(value, dict) for value in row.values()), "nested participant configuration is forbidden")
            if "token_sha256" in row:
                require(isinstance(row["token_sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", row["token_sha256"]) is not None,
                        "participant backup requires token hashes, never reusable tokens")


def validate_database(path: Path) -> None:
    from gremlin_town.storage.migrations import MIGRATIONS, SCHEMA_VERSION

    # Compare the entire schema with this release's migrations. This refuses an
    # OAuth DB renamed as a work DB, extra session tables, and injected triggers.
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        version = db.execute("PRAGMA user_version").fetchone()[0]
        require(1 <= version <= SCHEMA_VERSION, "unsupported work database schema")
        with closing(sqlite3.connect(":memory:")) as expected:
            for number in range(1, version + 1):
                expected.executescript(MIGRATIONS[number])
            query = "SELECT type, name, tbl_name, sql FROM sqlite_schema ORDER BY type, name"
            require(db.execute(query).fetchall() == expected.execute(query).fetchall(),
                    "database is not the exact work schema; authentication/session state is forbidden")
        require(db.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "work database integrity check failed")


def validate_directory(directory: Path) -> None:
    require({path.name for path in directory.iterdir()} == FILES, "backup must contain exactly the five allowed files")
    for name in FILES:
        path = directory / name
        require(path.is_file() and not path.is_symlink(), "backup links and non-files are forbidden")
        require(path.stat().st_nlink == 1, "backup hard links are forbidden")
        require(path.stat().st_size <= (DATABASE_LIMIT if name.endswith(".sqlite3") else TEXT_LIMIT), "backup member exceeds size limit")
    checksums: dict[str, str] = {}
    for line in (directory / "SHA256SUMS").read_text(encoding="ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]+)", line)
        require(match is not None, "invalid checksum entry")
        digest, name = match.groups()
        require(name in HASHED_FILES and name not in checksums, "unexpected or duplicate checksum entry")
        checksums[name] = digest
    require(set(checksums) == HASHED_FILES, "missing checksum entry")
    for name, digest in checksums.items():
        with (directory / name).open("rb") as handle:
            require(hashlib.file_digest(handle, "sha256").hexdigest() == digest, "backup checksum mismatch")
    validate_config((directory / "config.toml").read_bytes(), (directory / "participants.toml").read_bytes())
    validate_database(directory / "gremlin-town.sqlite3")


def extract_archive(archive: Path, target: Path) -> None:
    require(not target.exists() and not target.is_symlink(), "restore target must not exist")
    # Keep one archive handle open for inventory and reads. No tar extraction API
    # is used: only exact flat allowlisted names are ever copied to private staging.
    with tarfile.open(archive, "r:") as source:
        members = source.getmembers()
        require(len(members) == len(FILES) and {item.name for item in members} == FILES,
                "archive must contain exactly the five allowed names, with no duplicates")
        for item in members:
            require(item.isreg() and not item.issparse(), "archive links, sparse files and special files are forbidden")
            require(0 <= item.size <= (DATABASE_LIMIT if item.name.endswith(".sqlite3") else TEXT_LIMIT), "archive member exceeds size limit")
            require(set(item.pax_headers) <= {"mtime", "atime", "ctime"}, "unsupported archive metadata")
        # Header/name/type validation completes before any member is written.
        with tempfile.TemporaryDirectory(prefix=".gt-validate-", dir=target.parent) as staging:
            staged = Path(staging)
            for item in members:
                with source.extractfile(item) as incoming, (staged / item.name).open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                (staged / item.name).chmod(0o600)
            validate_directory(staged)
            target.mkdir(mode=0o700)  # Exclusive create, including after validation.
            for name in sorted(FILES):
                with (staged / name).open("rb") as incoming, (target / name).open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                (target / name).chmod(0o600)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("directory")
    validate.add_argument("directory", type=Path)
    extract = commands.add_parser("extract")
    extract.add_argument("archive", type=Path)
    extract.add_argument("target", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "directory":
            validate_directory(args.directory)
        else:
            extract_archive(args.archive, args.target)
    except (ValueError, OSError, sqlite3.Error, tarfile.TarError) as exc:
        # Do not echo file contents or credentials from malformed input.
        parser.exit(1, f"backup validation refused ({type(exc).__name__})\n")


if __name__ == "__main__":
    main()
