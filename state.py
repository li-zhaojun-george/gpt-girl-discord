"""Local state. User-owned facts, successful exchanges and explicit reminders."""
import json
import sqlite3
import time
from contextlib import contextmanager

class State:
    def __init__(self, path):
        self.path = str(path)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS facts (
                    scope TEXT, user TEXT, text TEXT, created REAL,
                    PRIMARY KEY(scope,user,text));
                CREATE TABLE IF NOT EXISTS exchanges (
                    id INTEGER PRIMARY KEY, scope TEXT, user TEXT, body TEXT, created REAL);
                CREATE TABLE IF NOT EXISTS privacy (
                    scope TEXT, user TEXT, muted INTEGER, PRIMARY KEY(scope,user));
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY, scope TEXT, user TEXT, text TEXT, due REAL,
                    status TEXT DEFAULT 'pending');
                CREATE TABLE IF NOT EXISTS quota (
                    day TEXT PRIMARY KEY, total INTEGER DEFAULT 0, social INTEGER DEFAULT 0);
                CREATE INDEX IF NOT EXISTS reminder_due ON reminders(status,due);
                CREATE INDEX IF NOT EXISTS exchange_scope ON exchanges(scope,user,created);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def muted(self, scope, user):
        with self.db() as db:
            row = db.execute("SELECT muted FROM privacy WHERE scope=? AND user=?", (scope, str(user))).fetchone()
            return bool(row and row[0])

    def set_muted(self, scope, user, muted):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO privacy VALUES (?,?,?)", (scope, str(user), int(muted)))

    def remember(self, scope, user, text):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO facts VALUES (?,?,?,?)", (scope, str(user), text, time.time()))
            db.execute("""DELETE FROM facts WHERE scope=? AND user=? AND rowid NOT IN
                          (SELECT rowid FROM facts WHERE scope=? AND user=? ORDER BY created DESC LIMIT 10)""",
                       (scope, str(user), scope, str(user)))

    def facts(self, scope, user):
        with self.db() as db:
            return [r[0] for r in db.execute("SELECT text FROM facts WHERE scope=? AND user=? ORDER BY created", (scope, str(user)))]

    def history(self, scope, user):
        self.prune()
        with self.db() as db:
            rows = db.execute("SELECT body FROM exchanges WHERE scope=? AND user=? ORDER BY id DESC LIMIT 6", (scope, str(user))).fetchall()
            return [json.loads(r[0]) for r in reversed(rows)]

    def save_exchange(self, scope, user, text, answer):
        with self.db() as db:
            db.execute("INSERT INTO exchanges(scope,user,body,created) VALUES (?,?,?,?)",
                       (scope, str(user), json.dumps({"user": text, "assistant": answer}, ensure_ascii=False), time.time()))
            db.execute("""DELETE FROM exchanges WHERE scope=? AND user=? AND id NOT IN
                          (SELECT id FROM exchanges WHERE scope=? AND user=? ORDER BY id DESC LIMIT 6)""",
                       (scope, str(user), scope, str(user)))

    def forget(self, scope, user):
        with self.db() as db:
            for table in ("facts", "exchanges", "reminders"):
                db.execute(f"DELETE FROM {table} WHERE scope=? AND user=?", (scope, str(user)))
            # Other replies may have paraphrased this user's observed messages.
            # Discard the channel's derived short-term exchanges as well.
            db.execute("DELETE FROM exchanges WHERE scope=?", (scope,))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_topics'").fetchone():
                # Topic snapshots combine multiple speakers; rebuild from fresh public messages.
                db.execute('DELETE FROM conversation_topics WHERE scope=?', (scope,))
            if db.execute("SELECT 1 FROM sqlite_master WHERE name='social_memories'").fetchone():
                ids = [r[0] for r in db.execute('''SELECT id FROM social_memories WHERE scope=? AND
                    (owner=? OR id IN (SELECT memory_id FROM memory_evidence WHERE user_id=?))''',
                    (scope,str(user),str(user)))]
                for memory_id in ids:
                    db.execute('DELETE FROM memory_evidence WHERE memory_id=?',(memory_id,))
                    db.execute('DELETE FROM social_memories WHERE id=?',(memory_id,))

    def prune(self):
        with self.db() as db:
            db.execute("DELETE FROM exchanges WHERE created<?", (time.time() - 1800,))
            db.execute("DELETE FROM quota WHERE day < date('now','-32 days')")
            db.execute("DELETE FROM reminders WHERE due<?", (time.time() - 86400 * 7,))

    def reserve_call(self, day, total_limit, social_limit, social=False):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT OR IGNORE INTO quota(day) VALUES (?)", (day,))
            row = db.execute("SELECT total,social FROM quota WHERE day=?", (day,)).fetchone()
            if ((total_limit >= 0 and row["total"] >= total_limit) or
                    (social and social_limit >= 0 and row["social"] >= social_limit)):
                return False
            db.execute("UPDATE quota SET total=total+1,social=social+? WHERE day=?", (int(social), day))
            return True

    def add_reminder(self, scope, user, text, due):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            count = db.execute("SELECT count(*) FROM reminders WHERE scope=? AND user=? AND status='pending'", (scope, str(user))).fetchone()[0]
            total = db.execute("SELECT count(*) FROM reminders WHERE status='pending'").fetchone()[0]
            if count >= 5 or total >= 1000:
                raise ValueError("待办提醒已达上限（每人每频道 5 条，全局 1000 条）")
            return db.execute("INSERT INTO reminders(scope,user,text,due) VALUES (?,?,?,?)", (scope, str(user), text, due)).lastrowid

    def reminders(self, scope, user):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM reminders WHERE scope=? AND user=? ORDER BY due DESC LIMIT 10", (scope, str(user)))]

    def cancel(self, scope, user, reminder_id):
        with self.db() as db:
            return db.execute("UPDATE reminders SET status='cancelled' WHERE id=? AND scope=? AND user=? AND status='pending'",
                              (reminder_id, scope, str(user))).rowcount > 0

    def due(self, now):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM reminders WHERE status='pending' AND due<=? ORDER BY due LIMIT 20", (now,))]

    def claim(self, reminder_id):
        # At most one delivery attempt. A crash after claim is shown as uncertain,
        # rather than replaying a potentially delivered Discord message on restart.
        with self.db() as db:
            return db.execute("UPDATE reminders SET status='uncertain' WHERE id=? AND status='pending'", (reminder_id,)).rowcount == 1

    def finish(self, reminder_id, status):
        with self.db() as db:
            db.execute("UPDATE reminders SET status=? WHERE id=?", (status, reminder_id))
