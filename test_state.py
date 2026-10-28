import json
import tempfile
import time
import unittest
from pathlib import Path
from state import State
from settings import Settings

class StateTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name) / "state.db"
        self.state = State(self.path)

    def tearDown(self):
        self.folder.cleanup()

    def test_restart_bounded_history_facts_and_retention(self):
        for n in range(14):
            self.state.remember("1:2", 3, str(n))
            self.state.save_exchange("1:2", 3, str(n), "ok")
        reopened = State(self.path)
        self.assertEqual(len(reopened.facts("1:2", 3)), 10)
        self.assertEqual(len(reopened.history("1:2", 3)), 6)
        with reopened.db() as db:
            db.execute("UPDATE exchanges SET created=?", (time.time()-1801,))
        self.assertEqual(reopened.history("1:2", 3), [])

    def test_quota_restart_day_and_social_reservation(self):
        self.assertTrue(self.state.reserve_call("2026-10-28", 2, 1, True))
        self.assertFalse(State(self.path).reserve_call("2026-10-28", 2, 1, True))
        self.assertTrue(self.state.reserve_call("2026-10-28", 2, 1))
        self.assertFalse(self.state.reserve_call("2026-10-28", 2, 1))
        self.assertTrue(self.state.reserve_call("2026-10-29", 2, 1, True))

    def test_reminder_claim_survives_restart_and_bound(self):
        for n in range(5):
            self.state.add_reminder("1:2", 3, str(n), time.time()-1)
        with self.assertRaises(ValueError):
            self.state.add_reminder("1:2", 3, "excess", time.time())
        rid = self.state.due(time.time())[0]["id"]
        self.assertTrue(self.state.claim(rid))
        self.assertFalse(State(self.path).claim(rid))
        self.assertNotIn(rid, [r["id"] for r in self.state.due(time.time())])

    def test_optout_survives_restart_and_forget_does_not_reset_quota(self):
        self.state.set_muted("1:2", 3, True)
        self.state.reserve_call("2026-10-28", 1, 0)
        self.state.forget("1:2", 3)
        self.assertTrue(State(self.path).muted("1:2", 3))
        self.assertFalse(self.state.reserve_call("2026-10-28", 1, 0))

    def test_forget_also_removes_derived_channel_exchanges(self):
        self.state.save_exchange("1:2", 4, "What did user 3 say?", "derived content")
        self.state.save_exchange("1:9", 4, "unrelated channel", "keep")
        self.state.remember("1:2", 4, "user 4 explicitly saved this")
        self.state.forget("1:2", 3)
        self.assertEqual(self.state.history("1:2", 4), [])
        self.assertEqual(len(self.state.history("1:9", 4)), 1)
        self.assertEqual(len(self.state.facts("1:2", 4)), 1)

class SettingsTests(unittest.TestCase):
    def test_invalid_scope_mode_limits_fail_closed(self):
        for kwargs in [dict(allowed=set()), dict(allowed={1}, observed={2}),
                       dict(allowed={1}, mode="unknown"), dict(allowed={1}, backend="auto"),
                       dict(allowed={1}, daily_calls=2, social_calls=3),
                       dict(allowed={1}, social_gap=0)]:
            with self.assertRaises(ValueError):
                Settings(**kwargs)

    def test_quiet_hours_cross_midnight(self):
        cfg = Settings({1})
        self.assertTrue(cfg.quiet(23))
        self.assertTrue(cfg.quiet(7))
        self.assertFalse(cfg.quiet(8))
        cfg.quiet_end = cfg.quiet_start
        self.assertFalse(cfg.quiet(23))

