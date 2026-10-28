import tempfile
import unittest
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS
from budget import DailyBudget, LIMIT, EXHAUSTED


class BudgetTests(unittest.TestCase):
    def test_legacy_currency_migration_is_once_and_preserves_usage(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'legacy.db'
            db = sqlite3.connect(path)
            db.executescript("CREATE TABLE days (day TEXT PRIMARY KEY, used INTEGER NOT NULL); INSERT INTO days VALUES ('2026-10-25',100000000); CREATE TABLE calls (id INTEGER PRIMARY KEY, day TEXT, reserved INTEGER, settled INTEGER DEFAULT 0); INSERT INTO calls VALUES (1,'2026-10-25',10000,0); CREATE TABLE blocked (day TEXT PRIMARY KEY); INSERT INTO blocked VALUES ('2026-10-25');")
            db.close()
            for _ in range(2):
                b = DailyBudget(path, lambda: '2026-10-25')
                self.assertFalse(b.exhausted())
                with b.connect() as db:
                    self.assertEqual(db.execute('SELECT used FROM days').fetchone()[0], 20000000)
                    self.assertEqual(db.execute('SELECT reserved FROM calls').fetchone()[0], 2000)

    def test_persistence_settlement_and_new_day(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'budget.db'
            day = ['2026-10-25']
            b = DailyBudget(path, lambda: day[0])
            ticket = b.reserve('persona', [], 600)
            b.settle(ticket, NS(input_tokens=10, output_tokens=5))
            b.settle(ticket, NS(input_tokens=10, output_tokens=5))
            with b.connect() as db:
                self.assertEqual(db.execute('SELECT used FROM days').fetchone()[0], 6000)
            while b.reserve('persona', [], 600) is not None:
                pass
            self.assertFalse(DailyBudget(path, lambda: day[0]).exhausted())
            with b.connect() as db:
                self.assertLessEqual(db.execute('SELECT used FROM days').fetchone()[0], LIMIT)
            day[0] = '2026-10-26'
            self.assertFalse(b.exhausted())
            self.assertIsNotNone(b.reserve('persona', [], 600))

    def test_oversized_request_does_not_lock_out_smaller_request(self):
        with tempfile.TemporaryDirectory() as folder:
            b = DailyBudget(Path(folder) / "budget.db")
            self.assertIsNone(b.reserve("字" * 200000, [], 600))
            self.assertFalse(b.exhausted())
            self.assertIsNotNone(b.reserve("small", [], 600))

    def test_unknown_usage_keeps_reservation(self):
        with tempfile.TemporaryDirectory() as folder:
            b = DailyBudget(Path(folder) / "budget.db")
            ticket = b.reserve("small", [], 600)
            b.settle(ticket, None)
            with b.connect() as db:
                row = db.execute("SELECT used FROM days").fetchone()
                self.assertGreater(row[0], 0)

