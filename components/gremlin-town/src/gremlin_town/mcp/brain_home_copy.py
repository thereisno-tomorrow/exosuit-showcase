"""The hub's copy of Brain Home, kept beside the Brain provider state.

GitHub stays the only authoritative Brain Home, and every save still goes there
first. This file keeps the contents of Brain Home files already read from GitHub
and checked there, keyed by their Git blob ID, so a later read downloads only
what changed. Every read still asks GitHub for the branch's latest version, and
Brain Core re-hashes each copy it loads. The file is not a canonical record and
may be deleted at any time; the next read downloads what it needs again. A
failure here is logged and never changes a Brain result.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

COPY_FILE = "brain-home-copy.sqlite3"
# With the copy, a scan costs one GitHub call plus any changed records, so the
# hosted limits sit far above Brain Core's uncached defaults (512 records, 8 MiB).
SCAN_MAX_RECORDS = 5000
SCAN_MAX_BYTES = 32 * 1024 * 1024


class SQLiteBlobStore:
    """Durable file copies keyed by Git blob ID; Brain Core verifies every load."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.Lock()
        self._database = sqlite3.connect(path, timeout=5, isolation_level=None, check_same_thread=False)
        try:
            self._database.execute("PRAGMA journal_mode=WAL")
            self._database.execute(
                "CREATE TABLE IF NOT EXISTS blobs (blob_sha TEXT PRIMARY KEY, content BLOB NOT NULL)"
            )
        except sqlite3.Error:
            self._database.close()
            raise

    def load(self, blob_sha: str) -> bytes | None:
        try:
            with self._lock:
                row = self._database.execute(
                    "SELECT content FROM blobs WHERE blob_sha = ?", (blob_sha,)
                ).fetchone()
        except sqlite3.Error as exc:
            logger.error("Brain Home copy read failed (%s)", type(exc).__name__)
            return None
        return None if row is None else bytes(row[0])

    def store(self, blob_sha: str, content: bytes) -> None:
        try:
            with self._lock:
                self._database.execute(
                    "INSERT OR REPLACE INTO blobs(blob_sha, content) VALUES (?, ?)", (blob_sha, content)
                )
        except sqlite3.Error as exc:
            logger.error("Brain Home copy write failed (%s)", type(exc).__name__)

    def close(self) -> None:
        with self._lock:
            self._database.close()

    async def aclose(self) -> None:
        self.close()


def open_copy_store(state_dir: Path) -> SQLiteBlobStore | None:
    """Open the copy, or log why not; reads then fall back to memory and GitHub."""

    try:
        return SQLiteBlobStore(state_dir / COPY_FILE)
    except (sqlite3.Error, OSError) as exc:
        logger.error("Brain Home copy unavailable; reads use GitHub and memory only (%s)", type(exc).__name__)
        return None
