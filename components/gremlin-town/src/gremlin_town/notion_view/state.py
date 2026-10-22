"""The view's own small SQLite file: which Notion page holds each item, what was last sent, and the last copy.

Never canonical: losing it only costs a rebuild (the next copy creates fresh rows, and the repair's sweep archives
the old ones). Each write commits at once, so a copy that fails halfway still remembers the pages it made.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


class ViewState:
    def __init__(self, path: str | Path) -> None:
        self._db = sqlite3.connect(str(path))
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS rows (
                root_id TEXT PRIMARY KEY,
                page_id TEXT NOT NULL,
                content_hash TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            """
        )
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    def rows(self) -> dict[str, tuple[str, str]]:
        """``root_id -> (page_id, content_hash)``."""

        return {root: (page, digest) for root, page, digest in self._db.execute("SELECT root_id, page_id, content_hash FROM rows")}

    def put(self, root_id: str, page_id: str, content_hash: str) -> None:
        self._db.execute(
            "INSERT INTO rows (root_id, page_id, content_hash) VALUES (?, ?, ?) "
            "ON CONFLICT(root_id) DO UPDATE SET page_id = excluded.page_id, content_hash = excluded.content_hash",
            (root_id, page_id, content_hash),
        )
        self._db.commit()

    def remove(self, root_id: str) -> None:
        self._db.execute("DELETE FROM rows WHERE root_id = ?", (root_id,))
        self._db.commit()

    def get(self, key: str) -> str | None:
        found = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if found is None else found[0]

    def set(self, key: str, value: str | None) -> None:
        self._db.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._db.commit()
