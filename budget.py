"""Persistent shared daily budget, in billionths of CAD (not billing data)."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

LIMIT = 100_000_000  # CAD 0.10
EXHAUSTED = '今天的额度用完啦，明天再聊～'
# Accounting exchange rate: 1.60 CAD/USD, fixed with a conservative buffer.
# Luna Standard input includes a 25% cache-write cushion; no cached discount.
INPUT_RATE = 200
OUTPUT_RATE = 800


class DailyBudget:
    def __init__(self, path, clock=None):
        self.path = str(path)
        self.clock = clock or (lambda: datetime.now(ZoneInfo('America/Toronto')).date().isoformat())
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('CREATE TABLE IF NOT EXISTS days (day TEXT PRIMARY KEY, used INTEGER NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS calls (id INTEGER PRIMARY KEY, day TEXT, reserved INTEGER, settled INTEGER DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS blocked (day TEXT PRIMARY KEY)')
            db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)')
            currency = db.execute("SELECT value FROM metadata WHERE key='currency'").fetchone()
            if currency is None:
                # Legacy ledger used 8 CNY/USD; 1.60 CAD/USD is exactly one fifth.
                # Preserve all usage and in-flight reservations; round up, never reset.
                db.execute('UPDATE days SET used=(used+4)/5')
                db.execute('UPDATE calls SET reserved=(reserved+4)/5')
                db.execute('DELETE FROM blocked WHERE day IN (SELECT day FROM days WHERE used < ?)', (LIMIT,))
                db.execute("INSERT INTO metadata VALUES ('currency','CAD')")
            elif currency[0] != 'CAD':
                raise ValueError('Unsupported budget currency')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def exhausted(self):
        with self.connect() as db:
            if db.execute('SELECT 1 FROM blocked WHERE day=?', (self.clock(),)).fetchone():
                return True
            row = db.execute('SELECT used FROM days WHERE day=?', (self.clock(),)).fetchone()
            return bool(row and row[0] >= LIMIT)

    def reserve(self, instructions, items, max_output):
        # Text-only byte bound with generous framing margin. Reserve before I/O.
        size = len(json.dumps([instructions, items], ensure_ascii=False).encode('utf-8'))
        amount = (size * 2 + 4096) * INPUT_RATE + max_output * OUTPUT_RATE
        day = self.clock()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute('SELECT 1 FROM blocked WHERE day=?', (day,)).fetchone():
                return None
            db.execute('INSERT OR IGNORE INTO days VALUES (?,0)', (day,))
            used = db.execute('SELECT used FROM days WHERE day=?', (day,)).fetchone()[0]
            if used + amount > LIMIT:
                db.execute('INSERT OR IGNORE INTO blocked VALUES (?)', (day,))
                return None
            db.execute('UPDATE days SET used=used+? WHERE day=?', (amount, day))
            return db.execute('INSERT INTO calls(day,reserved) VALUES (?,?)', (day, amount)).lastrowid

    def settle(self, call_id, usage):
        if usage is None:
            return  # Failed/unknown outcomes retain their full reservation, even after restart.
        cost = int(usage.input_tokens) * INPUT_RATE + int(usage.output_tokens) * OUTPUT_RATE
        if cost < 0:
            raise ValueError('Invalid usage')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            day, reserved, settled = db.execute('SELECT day,reserved,settled FROM calls WHERE id=?', (call_id,)).fetchone()
            if not settled:
                db.execute('UPDATE days SET used=used+? WHERE day=?', (cost-reserved, day))
                db.execute('UPDATE calls SET settled=1 WHERE id=?', (call_id,))
