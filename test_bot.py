import asyncio
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from bot import GirlBot
from providers import QuotaError
from settings import Settings
from state import State

class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.state = State(Path(self.folder.name) / "state.db")
        self.provider = NS(complete=AsyncMock(return_value=("reply", "你好。")))
        self.bot = GirlBot(self.provider, Settings({20}, debounce_seconds=0), "测试人格", self.state)
        self.bot._connection.user = NS(id=99)
        self.bot.now = lambda: datetime(2026, 10, 28, 12, tzinfo=ZoneInfo("America/Toronto"))

    async def asyncTearDown(self):
        await self.bot.close()
        self.folder.cleanup()

    def message(self, text="<@99> 你好", user=1, channel=20, **extra):
        values = dict(content=text, author=NS(id=user, bot=False, display_name="群友"),
                      webhook_id=None, guild=NS(id=10), reference=None,
                      channel=NS(id=channel), reply=AsyncMock())
        values.update(extra)
        return NS(**values)

    async def test_ignores_unrelated_channels_bots_dms_and_webhooks(self):
        for msg in [self.message("普通聊天"), self.message("@everyone"),
                    self.message(channel=21), self.message(guild=None),
                    self.message(webhook_id=1), self.message(author=NS(bot=True))]:
            await self.bot.on_message(msg)
            msg.reply.assert_not_awaited()
        self.provider.complete.assert_not_awaited()

    async def test_direct_reply_and_mentions_disable_pings(self):
        msg = self.message("接着说", reference=NS(resolved=NS(author=NS(id=99), channel=NS(id=20))))
        await self.bot.on_message(msg)
        self.assertTrue(self.provider.complete.call_args.args[1]["direct"])
        self.assertFalse(msg.reply.call_args.kwargs["mention_author"])
        self.assertFalse(msg.reply.call_args.kwargs["allowed_mentions"].everyone)
        self.assertEqual(len(self.state.history("10:20", 1)), 1)

    async def test_history_and_facts_are_scoped(self):
        self.state.remember("10:20", 1, "喜欢棋类")
        self.state.remember("10:21", 2, "不能泄漏")
        await self.bot.on_message(self.message(user=2))
        payload = self.provider.complete.call_args.args[1]
        self.assertEqual(payload["facts"], [])
        self.assertEqual(payload["history"], [])

    async def test_successful_send_only_is_recorded(self):
        with patch.object(self.bot, "respond", AsyncMock(return_value=False)):
            await self.bot.on_message(self.message())
        self.assertEqual(self.state.history("10:20", 1), [])

    async def test_duplicate_event_and_oversized_input_do_not_repeat_model(self):
        msg = self.message()
        msg.id = 456
        await self.bot.on_message(msg)
        await self.bot.on_message(msg)
        await self.bot.on_message(self.message("<@99> " + "字"*2001, user=2))
        self.assertEqual(self.provider.complete.await_count, 1)

    async def test_errors_release_capacity_without_leaking_details(self):
        self.provider.complete.side_effect = RuntimeError("secret response")
        msg = self.message()
        await self.bot.on_message(msg)
        self.assertFalse(self.bot.model_busy)
        self.assertNotIn("secret", msg.reply.call_args.args[0])
        self.assertEqual(self.state.history("10:20", 1), [])

    async def test_forget_works_while_busy_and_quota_exhausted(self):
        self.state.remember("10:20", 1, "秘密")
        self.state.remember("10:20", 2, "他人的")
        self.state.add_reminder("10:20", 1, "明天考试", time.time()+60)
        self.bot.model_busy = True
        self.provider.complete.side_effect = QuotaError()
        await self.bot.on_message(self.message("<@99> 清空记忆"))
        self.assertEqual(self.state.facts("10:20", 1), [])
        self.assertEqual(self.state.reminders("10:20", 1), [])
        self.assertEqual(self.state.facts("10:20", 2), ["他人的"])
        self.provider.complete.assert_not_awaited()

    async def test_forget_invalidates_inflight_answer(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def complete(*args):
            entered.set()
            await release.wait()
            return "reply", "过期内容"
        self.provider.complete.side_effect = complete
        msg = self.message()
        task = asyncio.create_task(self.bot.on_message(msg))
        await entered.wait()
        await self.bot.on_message(self.message("<@99> 清空记忆"))
        release.set()
        await task
        msg.reply.assert_not_awaited()
        self.assertEqual(self.state.history("10:20", 1), [])

    async def test_privacy_optout_prevents_observation_and_model_calls(self):
        self.bot.settings.mode, self.bot.settings.observed = "observe", {20}
        await self.bot.on_message(self.message("<@99> 不记录我"))
        await self.bot.on_message(self.message("不应保存"))
        await self.bot.on_message(self.message("<@99> 也不应发送"))
        self.assertNotIn("10:20", self.bot.context)
        self.provider.complete.assert_not_awaited()
        await self.bot.on_message(self.message("<@99> 恢复记录"))
        await self.bot.on_message(self.message())
        self.provider.complete.assert_awaited_once()

    async def test_observe_reads_only_configured_channel_without_speaking(self):
        self.bot.settings.mode, self.bot.settings.observed = "observe", {20}
        await self.bot.on_message(self.message("刚看完电影"))
        self.provider.complete.assert_not_awaited()
        await self.bot.on_message(self.message())
        self.assertEqual(self.provider.complete.call_args.args[1]["recent"][0]["text"], "刚看完电影")

    async def test_social_silence_still_counts_and_cools_down(self):
        self.bot.settings.mode = "social"
        self.provider.complete.return_value = ("silence", "")
        msg = self.message("路过")
        await self.bot.generate(msg, "路过", direct=False)
        await self.bot.generate(msg, "路过", direct=False)
        self.provider.complete.assert_awaited_once()
        msg.reply.assert_not_awaited()
        self.assertEqual(self.state.history("10:20", 1), [])

    async def test_social_quiet_and_budget(self):
        self.bot.settings.quiet_start, self.bot.settings.quiet_end = 10, 13
        await self.bot.generate(self.message(), "路过", direct=False)
        self.provider.complete.assert_not_awaited()
        self.bot.settings.quiet_start = self.bot.settings.quiet_end = 0
        self.bot.settings.social_calls = 0
        await self.bot.generate(self.message(), "路过", direct=False)
        self.provider.complete.assert_not_awaited()

    async def test_social_duplicate_suppressed(self):
        self.bot.recent_bot["10:20"] = [{"text": "你好。", "time": time.time()}]
        msg = self.message("普通消息")
        await self.bot.generate(msg, msg.content, direct=False)
        msg.reply.assert_not_awaited()

    async def test_pending_social_turn_is_cancelled_by_forget_epoch(self):
        self.bot.settings.mode, self.bot.settings.observed = "social", {20}
        await self.bot.on_message(self.message("第一条"))
        await self.bot.on_message(self.message("第二条", user=2))
        self.assertEqual(len(self.bot.pending), 2)
        await self.bot.on_message(self.message("<@99> 清空记忆"))
        await asyncio.gather(*list(self.bot.pending.values()))
        self.provider.complete.assert_not_awaited()

    async def test_explicit_reminder_create_cancel_and_scope(self):
        await self.bot.on_message(self.message("<@99> 提醒 30 复习"))
        rows = self.state.reminders("10:20", 1)
        self.assertEqual(rows[0]["text"], "复习")
        await self.bot.on_message(self.message(f"<@99> 取消提醒 {rows[0]['id']}", user=2))
        self.assertEqual(self.state.reminders("10:20", 1)[0]["status"], "pending")
        await self.bot.on_message(self.message(f"<@99> 取消提醒 {rows[0]['id']}"))
        self.assertEqual(self.state.reminders("10:20", 1)[0]["status"], "cancelled")
        self.provider.complete.assert_not_awaited()

    async def test_reminder_delivery_no_ping_no_replay(self):
        rid = self.state.add_reminder("10:20", 1, "考试后休息", self.bot.now().timestamp()-60)
        channel = NS(guild=NS(id=10, me=NS()), send=AsyncMock(),
                     permissions_for=lambda _: NS(view_channel=True, send_messages=True))
        with patch.object(self.bot, "get_channel", return_value=channel):
            await self.bot.deliver_reminders()
            await self.bot.deliver_reminders()
        channel.send.assert_awaited_once()
        self.assertFalse(channel.send.call_args.kwargs["allowed_mentions"].users)
        self.assertEqual(self.state.reminders("10:20", 1)[0]["status"], "sent")
        self.assertFalse(self.state.claim(rid))

    async def test_reminder_quiet_permission_and_expiry(self):
        now = self.bot.now().timestamp()
        self.state.add_reminder("10:20", 1, "待处理", now-60)
        channel = NS(guild=NS(id=10, me=NS()), send=AsyncMock(),
                     permissions_for=lambda _: NS(view_channel=True, send_messages=False))
        with patch.object(self.bot, "get_channel", return_value=channel):
            await self.bot.deliver_reminders()
        self.assertEqual(self.state.reminders("10:20", 1)[0]["status"], "pending")
        self.bot.settings.quiet_start, self.bot.settings.quiet_end = 10, 13
        with patch.object(self.bot, "get_channel", return_value=channel):
            await self.bot.deliver_reminders()
        channel.send.assert_not_awaited()
        self.bot.settings.quiet_start = self.bot.settings.quiet_end = 0
        self.state.add_reminder("10:20", 2, "过期了", now-90000)
        await self.bot.deliver_reminders()
        self.assertEqual(self.state.reminders("10:20", 2)[0]["status"], "expired")

    async def test_expired_observation_and_reply_cache_are_pruned(self):
        from collections import deque
        self.bot.context["10:20"] = deque([{"text": "old", "time": time.time()-1801}])
        self.bot.recent_bot["10:20"] = [{"text": "old answer", "time": time.time()-1801}]
        self.bot.prune_context()
        self.assertNotIn("10:20", self.bot.context)
        self.assertNotIn("10:20", self.bot.recent_bot)

    async def test_uncertain_send_is_not_retried(self):
        import discord
        self.state.add_reminder("10:20", 1, "once", self.bot.now().timestamp()-60)
        error = discord.HTTPException(NS(status=500, reason="error"), "unknown")
        channel = NS(guild=NS(id=10, me=NS()), send=AsyncMock(side_effect=error),
                     permissions_for=lambda _: NS(view_channel=True, send_messages=True))
        with patch.object(self.bot, "get_channel", return_value=channel):
            await self.bot.deliver_reminders()
            await self.bot.deliver_reminders()
        channel.send.assert_awaited_once()
        self.assertEqual(self.state.reminders("10:20", 1)[0]["status"], "uncertain")


    async def drain(self):
        if self.bot.pending:
            await asyncio.gather(*list(self.bot.pending.values()))

    def social(self):
        self.bot.settings.mode, self.bot.settings.observed = "social", {20}

    async def test_followup_bypasses_ambient_cooldown_quiet_and_zero_social_quota(self):
        self.social()
        self.bot.settings.social_calls = 0
        self.bot.settings.quiet_start, self.bot.settings.quiet_end = 10, 13
        await self.bot.on_message(self.message("<@99> 喜欢吃什么"))
        await self.bot.on_message(self.message("为什么喜欢这个"))
        await self.drain()
        self.assertEqual(self.provider.complete.await_count, 2)
        payload = self.provider.complete.call_args.args[1]
        self.assertEqual(payload["route"], "followup")
        self.assertEqual(len(payload["history"]), 1)
        with self.state.db() as db:
            self.assertEqual(tuple(db.execute("SELECT total,social FROM quota").fetchone()), (2,0))

    async def test_alias_is_addressed_without_spending_social_quota(self):
        self.social()
        self.bot.settings.social_calls = 0
        await self.bot.on_message(self.message("大白龙你喜欢吃什么"))
        await self.drain()
        self.assertEqual(self.provider.complete.call_args.args[1]["route"], "named")

    async def test_messages_to_others_do_not_call_model_or_keep_focus(self):
        self.social()
        await self.bot.on_message(self.message())
        await self.bot.on_message(self.message("<@123> 你可以不用@试试看"))
        await self.drain()
        self.assertEqual(self.provider.complete.await_count, 1)
        self.assertNotIn(("10:20", 1), self.bot.router.active)
        await self.bot.on_message(self.message("你确定吗", reference=NS(
            resolved=NS(author=NS(id=123), channel=NS(id=20)))))
        await self.drain()
        self.assertEqual(self.provider.complete.await_count, 1)

    async def test_implicit_burst_merges_per_person_and_keeps_latest_anchor(self):
        self.social()
        self.bot.router.touch("10:20", 1)
        first = self.message("那为什么")
        second = self.message("我是说PC这个版本")
        await self.bot.on_message(first)
        await self.bot.on_message(second)
        await self.drain()
        self.provider.complete.assert_awaited_once()
        self.assertIn("那为什么\n我是说PC这个版本", self.provider.complete.call_args.args[1]["text"])
        first.reply.assert_not_awaited()
        second.reply.assert_awaited_once()

    async def test_distinct_explicit_messages_not_silently_dropped_by_old_eight_second_rule(self):
        await self.bot.on_message(self.message("<@99> 第一问"))
        await self.bot.on_message(self.message("<@99> 第二问"))
        self.assertEqual(self.provider.complete.await_count, 2)

    async def test_conversation_waits_for_model_slot(self):
        entered, release = asyncio.Event(), asyncio.Event()
        async def complete(*args):
            if self.provider.complete.await_count == 1:
                entered.set()
                await release.wait()
            return "reply", "已回答"
        self.provider.complete.side_effect = complete
        one, two = self.message(user=1), self.message(user=2)
        first = asyncio.create_task(self.bot.on_message(one))
        await entered.wait()
        second = asyncio.create_task(self.bot.on_message(two))
        await asyncio.sleep(0)
        self.assertEqual(self.provider.complete.await_count, 1)
        release.set()
        await asyncio.gather(first, second)
        self.assertEqual(self.provider.complete.await_count, 2)
        one.reply.assert_awaited_once()
        two.reply.assert_awaited_once()

    async def test_newer_same_user_correction_discards_stale_model_answer(self):
        self.social()
        entered, release = asyncio.Event(), asyncio.Event()
        async def complete(*args):
            if self.provider.complete.await_count == 1:
                entered.set()
                await release.wait()
                return "reply", "旧答案"
            return "reply", "最新答案"
        self.provider.complete.side_effect = complete
        old = self.message("<@99> 主机数值是多少")
        task = asyncio.create_task(self.bot.on_message(old))
        await entered.wait()
        new = self.message("不对，我要问PC")
        await self.bot.on_message(new)
        release.set()
        await task
        await self.drain()
        old.reply.assert_not_awaited()
        self.assertEqual(new.reply.call_args.args[0], "最新答案")
        self.assertEqual(len(self.state.history("10:20", 1)), 1)

    async def test_other_addressee_invalidates_inflight_answer(self):
        self.social()
        entered, release = asyncio.Event(), asyncio.Event()
        async def complete(*args):
            entered.set()
            await release.wait()
            return "reply", "不应发送"
        self.provider.complete.side_effect = complete
        old = self.message()
        task = asyncio.create_task(self.bot.on_message(old))
        await entered.wait()
        await self.bot.on_message(self.message("<@123> 我改问你了"))
        release.set()
        await task
        old.reply.assert_not_awaited()

    async def test_explicit_end_stops_implicit_followup(self):
        self.social()
        await self.bot.on_message(self.message())
        await self.bot.on_message(self.message("不用回复了"))
        await self.drain()
        self.assertNotIn(("10:20", 1), self.bot.router.active)
        self.assertEqual(self.provider.complete.await_count, 1)

    async def test_error_backoff_prevents_request_storm(self):
        self.provider.complete.side_effect = RuntimeError("upstream")
        await self.bot.on_message(self.message())
        await self.bot.on_message(self.message(user=2))
        self.provider.complete.assert_awaited_once()
        self.assertFalse(self.bot.model_lock.locked())

    async def test_followup_focus_isolated_by_channel_and_user(self):
        self.social()
        await self.bot.on_message(self.message())
        self.bot.settings.allowed.add(21)
        self.bot.settings.observed.add(21)
        await self.bot.on_message(self.message("为什么", user=2))
        await self.bot.on_message(self.message("为什么", channel=21))
        await self.drain()
        # user2 in the same channel is suppressed by ambient cooldown;
        # another channel is ambient rather than a continuation of user1's chat.
        self.assertEqual(self.provider.complete.call_args.args[1]["route"], "ambient")
        self.assertEqual(self.provider.complete.call_args.args[1]["history"], [])

    async def test_forget_drops_pending_implicit_fragments(self):
        self.social()
        self.bot.router.touch("10:20", 1)
        await self.bot.on_message(self.message("旧的私密内容"))
        await self.bot.on_message(self.message("<@99> 清空记忆"))
        await self.drain()
        self.provider.complete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

