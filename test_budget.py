import tempfile
import unittest
import sqlite3
from pathlib import Path
from types import SimpleNamespace as NS
from budget import DailyBudget, LIMIT, EXHAUSTED
from test_bot import BotTests


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
            self.assertTrue(DailyBudget(path, lambda: day[0]).exhausted())
            with b.connect() as db:
                self.assertLessEqual(db.execute('SELECT used FROM days').fetchone()[0], LIMIT)
            day[0] = '2026-10-26'
            self.assertFalse(b.exhausted())
            self.assertIsNotNone(b.reserve('persona', [], 600))


class BudgetBotTests(BotTests):
    async def test_exhaustion_repeats_without_api_even_in_cooldown(self):
        with tempfile.TemporaryDirectory() as folder:
            self.bot.budget = DailyBudget(Path(folder) / 'budget.db', lambda: '2026-10-25')
            with self.bot.budget.connect() as db:
                db.execute('INSERT INTO days VALUES (?,?)', ('2026-10-25', LIMIT))
            for _ in range(2):
                msg = self.message()
                await self.bot.on_message(msg)
                self.assertEqual(msg.reply.call_args.args[0], EXHAUSTED)
            self.api.responses.create.assert_not_awaited()
