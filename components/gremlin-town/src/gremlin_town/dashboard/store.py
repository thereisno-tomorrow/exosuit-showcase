"""Hub-local authentication state. Transactions arbitrate all one-use operations.

No Brain records, raw session/enrollment tokens, IPs or request bodies are stored.
The private directory must be owned by the service account on the hub.
"""
from contextlib import contextmanager
import hashlib
import os
import secrets
import sqlite3
from pathlib import Path


class AuthRejected(ValueError):
    """Intentionally carries no credential or request detail."""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AuthStore:
    def __init__(self, path: Path, *, owner: str, origin: str, rp_id: str):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt" and path.parent.stat().st_mode & 0o077:
            raise ValueError("Dashboard authentication directory must be private to its owner")
        self.path = path
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS enrollment (token_hash TEXT PRIMARY KEY, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS browsers (token_hash TEXT PRIMARY KEY, expires INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS challenges (id TEXT PRIMARY KEY, browser_hash TEXT NOT NULL,
                    kind TEXT NOT NULL, challenge BLOB NOT NULL, expires INTEGER NOT NULL, enrollment_hash TEXT);
                CREATE TABLE IF NOT EXISTS credentials (id BLOB PRIMARY KEY, public_key BLOB NOT NULL,
                    sign_count INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires INTEGER NOT NULL,
                    credential_id BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS audit (at INTEGER NOT NULL, event TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS rate_limits (bucket INTEGER PRIMARY KEY, count INTEGER NOT NULL);
            """)
        with self.transaction() as db:
            for key, expected in (("owner", owner), ("origin", origin), ("rp_id", rp_id)):
                row = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
                if row and row[0] != expected:
                    raise ValueError("Dashboard authentication identity changed; operator review required")
                db.execute("INSERT OR IGNORE INTO config VALUES (?, ?)", (key, expected))
            db.execute("INSERT OR IGNORE INTO config VALUES ('user_handle', ?)", (secrets.token_hex(32),))
        path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def user_handle(self) -> bytes:
        with self.connect() as db:
            return bytes.fromhex(db.execute("SELECT value FROM config WHERE key='user_handle'").fetchone()[0])

    def audit(self, now: int, event: str) -> None:
        if event not in {"enrollment_issued", "enrollment_ok", "enrollment_failed", "sign_in_ok",
                         "sign_in_failed", "signed_out", "revoked_all"}:
            raise ValueError("Unknown authentication event")
        with self.transaction() as db:
            db.execute("INSERT INTO audit VALUES (?, ?)", (now, event))

    def rate_limit(self, now: int) -> bool:
        # Proposed: 120 anonymous ceremonies per minute across this one-owner app.
        with self.transaction() as db:
            bucket = now // 60
            db.execute("DELETE FROM rate_limits WHERE bucket < ?", (bucket,))
            db.execute("INSERT INTO rate_limits VALUES (?, 1) ON CONFLICT(bucket) DO UPDATE SET count=count+1", (bucket,))
            permitted = db.execute("SELECT count FROM rate_limits WHERE bucket=?", (bucket,)).fetchone()[0] <= 120
            for table in ("enrollment", "browsers", "challenges", "sessions"):
                db.execute(f"DELETE FROM {table} WHERE expires <= ?", (now,))
            db.execute("DELETE FROM audit WHERE at < ?", (now - 30 * 86400,))
            return permitted

    def browser(self, token: str, now: int, *, create: bool = False) -> bool:
        with self.transaction() as db:
            if create:
                db.execute("INSERT INTO browsers VALUES (?, ?)", (digest(token), now + 300))
                return True
            return db.execute("SELECT 1 FROM browsers WHERE token_hash=? AND expires>?", (digest(token), now)).fetchone() is not None

    def issue_enrollment(self, token: str, now: int):
        with self.transaction() as db:
            db.execute("INSERT INTO enrollment VALUES (?, ?)", (digest(token), now + 900))

    def enrollment_valid(self, token_hash: str, now: int) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM enrollment WHERE token_hash=? AND expires>?", (token_hash, now)).fetchone() is not None

    def add_challenge(self, browser: str, kind: str, challenge: bytes, now: int, enrollment_hash=None) -> str:
        challenge_id = secrets.token_urlsafe(32)
        with self.transaction() as db:
            db.execute("INSERT INTO challenges VALUES (?, ?, ?, ?, ?, ?)",
                       (challenge_id, digest(browser), kind, challenge, now + 300, enrollment_hash))
        return challenge_id

    def consume_challenge(self, challenge_id: str, browser: str, kind: str, now: int):
        with self.transaction() as db:
            row = db.execute("SELECT * FROM challenges WHERE id=? AND browser_hash=? AND kind=? AND expires>?",
                             (challenge_id, digest(browser), kind, now)).fetchone()
            if row is None:
                raise AuthRejected()
            db.execute("DELETE FROM challenges WHERE id=?", (challenge_id,))
            return row

    def credential(self, credential_id: bytes):
        with self.connect() as db:
            return db.execute("SELECT * FROM credentials WHERE id=? AND revoked=0", (credential_id,)).fetchone()

    def credential_ids(self):
        with self.connect() as db:
            return tuple(row[0] for row in db.execute("SELECT id FROM credentials WHERE revoked=0"))

    def register(self, enrollment_hash: str, verified, now: int):
        with self.transaction() as db:
            changed = db.execute("DELETE FROM enrollment WHERE token_hash=? AND expires>?", (enrollment_hash, now)).rowcount
            if changed != 1:
                raise AuthRejected()
            try:
                db.execute("INSERT INTO credentials(id,public_key,sign_count) VALUES (?,?,?)",
                           (verified.credential_id, verified.credential_public_key, verified.sign_count))
            except sqlite3.IntegrityError:
                raise AuthRejected() from None

    def update_counter(self, credential_id: bytes, previous: int, current: int):
        with self.transaction() as db:
            if db.execute("UPDATE credentials SET sign_count=? WHERE id=? AND sign_count=? AND revoked=0",
                          (current, credential_id, previous)).rowcount != 1:
                raise AuthRejected()

    def new_session(self, token: str, credential_id: bytes, expires: int):
        with self.transaction() as db:
            if db.execute("SELECT 1 FROM credentials WHERE id=? AND revoked=0", (credential_id,)).fetchone() is None:
                raise AuthRejected()
            db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (digest(token), expires, credential_id))

    def valid_session(self, token: str, expires: int, now: int) -> bool:
        with self.connect() as db:
            return db.execute("SELECT 1 FROM sessions s JOIN credentials c ON c.id=s.credential_id "
                              "WHERE s.token_hash=? AND s.expires=? AND s.expires>? AND c.revoked=0",
                              (digest(token), expires, now)).fetchone() is not None

    def logout(self, token: str):
        with self.transaction() as db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (digest(token),))

    def revoke_all(self):
        with self.transaction() as db:
            db.execute("UPDATE credentials SET revoked=1")
            db.execute("DELETE FROM sessions")
            db.execute("DELETE FROM challenges")
            db.execute("DELETE FROM enrollment")
