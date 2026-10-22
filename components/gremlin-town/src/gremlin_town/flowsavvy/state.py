"""Hub-local mapping, durable creation holds, schedule, and wake counter.

The state directory is private and backed up with the hub. Nothing is written to
Brain Home, and no Brain ID is sent to FlowSavvy. One file lock serializes syncs.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3

from .client import FlowSavvyError


class SyncState:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path, timeout=30)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS tasks (item_ref TEXT PRIMARY KEY, remote TEXT, phase TEXT NOT NULL, payload TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()
        if os.name != "nt":
            path.chmod(0o600)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.db.close()

    @contextmanager
    def sync_lock(self):
        with self.path.with_suffix(".lock").open("a+b") as lock:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                if lock.read(1) == b"":
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                try:
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError:
                    raise FlowSavvyError("FlowSavvy sync is already running") from None
            else:
                import fcntl
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    raise FlowSavvyError("FlowSavvy sync is already running") from None
            try:
                yield
            finally:
                if os.name == "nt":
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(lock, fcntl.LOCK_UN)

    def tasks(self) -> dict:
        return {ref: {"remote": remote, "phase": phase, "payload": json.loads(payload)}
                for ref, remote, phase, payload in self.db.execute("SELECT item_ref, remote, phase, payload FROM tasks")}

    def put(self, ref, remote, phase, payload):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO tasks VALUES (?, ?, ?, ?)", (ref, remote, phase, json.dumps(payload, sort_keys=True)))

    def remove(self, ref):
        with self.db:
            self.db.execute("DELETE FROM tasks WHERE item_ref = ?", (ref,))

    def set(self, key, value):
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, json.dumps(value)))

    def get(self, key, default=None):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def wake_version(self):
        return self.get("wake", 0)

    def wake(self):
        with self.db:
            self.db.execute("INSERT INTO meta VALUES ('wake', '1') ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + 1")


def notify_change(settings) -> None:
    """Call after a successful hub door save. No remote call, record data or IDs.

    Failure propagates; callers must report scheduling wake failure separately
    from the already-saved record, never retry or misreport the successful save.
    """
    if settings.enabled:
        with SyncState(settings.state_file) as state:
            state.wake()
