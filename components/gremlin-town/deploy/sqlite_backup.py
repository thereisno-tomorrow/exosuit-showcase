#!/usr/bin/env python3
"""Create and verify a consistent SQLite snapshot using the online backup API."""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: sqlite_backup.py SOURCE DESTINATION", file=sys.stderr)
        return 64
    source = Path(sys.argv[1]).resolve(strict=True)
    destination = Path(sys.argv[2])
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if destination.exists():
        print(f"refusing to overwrite {destination}", file=sys.stderr)
        return 73
    source_uri = f"file:{source.as_posix()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as src:
        with sqlite3.connect(destination) as dst:
            src.backup(dst, pages=256, sleep=0.05)
            result = dst.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise RuntimeError(f"snapshot integrity_check failed: {result!r}")
            dst.commit()
    os.chmod(destination, 0o600)
    with destination.open("rb") as handle:
        os.fsync(handle.fileno())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
