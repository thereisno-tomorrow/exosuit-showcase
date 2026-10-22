"""Hub-local navigation and exact retry intents, never a copy of record truth."""
from __future__ import annotations

import json
from pathlib import Path
import secrets
import sqlite3
import threading

from .models import CardError, initial_state


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class CardStore:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, timeout=10, check_same_thread=False)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS card_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS card_sessions(day TEXT PRIMARY KEY,state TEXT NOT NULL,delivery TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS card_actions(token TEXT PRIMARY KEY,day TEXT NOT NULL,spec TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ready', intent TEXT, result TEXT);
        """)

    def latest_timezone(self):
        with self.lock:
            saved = self.db.execute("SELECT value FROM card_meta WHERE key='timezone'").fetchone()
            if saved:
                return saved[0]
            rows = self.db.execute("SELECT state FROM card_sessions ORDER BY rowid DESC").fetchall()
            return next((value for row in rows if (value := json.loads(row[0]).get("timezone"))), "Asia/Singapore")

    def close(self):
        self.db.close()

    def session(self, day):
        day = str(day)
        with self.lock, self.db:
            active = self.db.execute("SELECT value FROM card_meta WHERE key='active_day'").fetchone()
            existing = self.db.execute("SELECT day,state FROM card_sessions WHERE day=?", (active[0],)).fetchone() if active else self.db.execute("SELECT day,state FROM card_sessions ORDER BY rowid DESC LIMIT 1").fetchone()
            seed = initial_state()
            if existing and existing["day"] != day:
                seed = json.loads(existing["state"])
                seed.update(date_changed=True, page=0, settling=False)
                seed.pop("day_approval", None)
                seed.pop("unknown_estimates", None)
                if not seed.get("interrupted_review") and seed.get("screen") in {"day", "settled", "lengths", "day_summary", "anything"}:
                    # A finished queue belongs to yesterday; choices remain kept.
                    seed.update(screen="open", stage="", item=None, skipped=[], history=[], page=0)
                    seed.pop("queue_snapshots", None)
                    seed.pop("queue_additions", None)
                elif seed.get("screen") in {"day", "settled", "lengths", "day_summary"}:
                    seed.update(screen="day", stage="day", item=None)
            self.db.execute("INSERT OR IGNORE INTO card_sessions VALUES (?,?,?)", (day, encoded(seed), "{}"))
            return json.loads(self.db.execute("SELECT state FROM card_sessions WHERE day=?", (day,)).fetchone()[0])

    def save(self, day, state):
        with self.lock, self.db:
            self.db.execute("UPDATE card_sessions SET state=? WHERE day=?", (encoded(state), str(day)))
            self.db.execute("INSERT OR REPLACE INTO card_meta VALUES ('active_day',?)", (str(day),))
            if state.get("timezone"):
                self.db.execute("INSERT OR REPLACE INTO card_meta VALUES ('timezone',?)", (state["timezone"],))

    def delivery(self, day):
        self.session(day)
        with self.lock:
            return json.loads(self.db.execute("SELECT delivery FROM card_sessions WHERE day=?", (str(day),)).fetchone()[0])

    def save_delivery(self, day, **values):
        with self.lock, self.db:
            current = self.delivery(day)
            current.update(values)
            self.db.execute("UPDATE card_sessions SET delivery=? WHERE day=?", (encoded(current), str(day)))
            return current

    def bind_message(self, day, message_id):
        delivery = self.save_delivery(day, message_id=message_id)
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO card_meta VALUES ('active_message',?)", (message_id,))
        return delivery

    def active_message_id(self):
        with self.lock:
            saved = self.db.execute("SELECT value FROM card_meta WHERE key='active_message'").fetchone()
            if saved:
                return saved[0]
            rows = self.db.execute("SELECT delivery FROM card_sessions ORDER BY day DESC").fetchall()
            return next((value for row in rows if (value := json.loads(row[0]).get("message_id"))), None)

    def issue(self, day, spec):
        token = "rc:" + secrets.token_urlsafe(18)
        with self.lock, self.db:
            self.db.execute("INSERT INTO card_actions(token,day,spec) VALUES (?,?,?)", (token, str(day), encoded(spec)))
        return token

    def action(self, token):
        with self.lock:
            row = self.db.execute("SELECT * FROM card_actions WHERE token=?", (token,)).fetchone()
        if row is None:
            raise CardError("That button is no longer available. Open today's review.")
        result = dict(row)
        for field in ("spec", "intent", "result"):
            result[field] = json.loads(result[field]) if result[field] else None
        return result

    def pending(self, day):
        with self.lock:
            row = self.db.execute("SELECT token FROM card_actions WHERE day<=? AND status='pending' ORDER BY rowid LIMIT 1", (str(day),)).fetchone()
        return self.action(row[0]) if row else None

    def bind_form(self, token):
        """Persist the opened form's authority; Discord sends words only on submit."""
        with self.lock, self.db:
            action = self.action(token)
            spec = dict(action["spec"], form_opened=True)
            self.db.execute("UPDATE card_actions SET spec=? WHERE token=?", (encoded(spec), token))

    def can_continue(self, token, *, submitted=False):
        try:
            action = self.action(token)
        except CardError:
            return False
        return action["status"] == "pending" or bool(submitted and action["spec"].get("form_opened"))

    def prepare(self, token, intent):
        with self.lock, self.db:
            action = self.action(token)
            if action["status"] != "ready":
                return action
            if self.pending(action["day"]):
                raise CardError("Check the earlier save first. It has not been confirmed yet.")
            self.db.execute("UPDATE card_actions SET status='pending',intent=? WHERE token=?", (encoded(intent), token))
        return self.action(token)

    def finish(self, token, result, state):
        with self.lock, self.db:
            action = self.action(token)
            self.db.execute("UPDATE card_actions SET status='done',result=? WHERE token=?", (encoded(result), token))
            self.db.execute("UPDATE card_sessions SET state=? WHERE day=?", (encoded(state), action["day"]))

    def refused(self, token, result):
        with self.lock, self.db:
            self.db.execute("UPDATE card_actions SET status='refused',result=? WHERE token=?", (encoded(result), token))
