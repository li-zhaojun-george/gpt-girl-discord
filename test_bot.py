import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from bot import GirlBot


class Typing:
    async def __aenter__(self):
        pass

    async def __aexit__(self, *args):
        pass


class BotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.api = NS(responses=NS(create=AsyncMock(return_value=NS(output_text="你好～"))))
        self.bot = GirlBot(self.api, "test-model", {20}, "test persona")
        self.bot._connection.user = NS(id=99)

    async def asyncTearDown(self):
        await self.bot.close()

    def message(self, content="<@99> 你好", user=1, channel=20, **extra):
        values = dict(content=content, author=NS(id=user, bot=False, display_name="群友"),
                      webhook_id=None, guild=NS(id=10),
                      channel=NS(id=channel, typing=Typing), reply=AsyncMock())
        values.update(extra)
        return NS(**values)

    async def test_ignores_non_mentions_dms_bots_and_other_channels(self):
        for msg in [self.message("普通聊天"), self.message("@everyone"),
                    self.message("回复机器人但正文没有提及"),
                    self.message(guild=None), self.message(webhook_id=123),
                    self.message(channel=21), self.message(author=NS(bot=True))]:
            await self.bot.on_message(msg)
            msg.reply.assert_not_awaited()
        self.api.responses.create.assert_not_awaited()

    async def test_mentions_call_api_and_disable_pings(self):
        msg = self.message("<@!99> 你好")
        await self.bot.on_message(msg)
        args = self.api.responses.create.call_args.kwargs
        self.assertFalse(args["store"])
        self.assertEqual(args["instructions"], "test persona")
        self.assertIn("你好", args["input"][0]["content"])
        self.assertFalse(msg.reply.call_args.kwargs["allowed_mentions"].everyone)
        self.assertFalse(msg.reply.call_args.kwargs["mention_author"])

    async def test_history_is_private_and_reset_is_scoped(self):
        await self.bot.on_message(self.message(user=1))
        await self.bot.on_message(self.message(user=2))
        self.assertEqual(len(self.api.responses.create.call_args.kwargs["input"]), 1)
        self.bot.cooldowns.clear()
        await self.bot.on_message(self.message("<@99> 接着说", user=1))
        self.assertEqual(len(self.api.responses.create.call_args.kwargs["input"]), 3)
        self.bot.cooldowns.clear()
        await self.bot.on_message(self.message("<@99> 清空记忆", user=1))
        self.assertNotIn((10, 20, 1), self.bot.sessions)
        self.assertIn((10, 20, 2), self.bot.sessions)

    async def test_cooldown_and_input_limit(self):
        await self.bot.on_message(self.message())
        await self.bot.on_message(self.message())
        self.assertEqual(self.api.responses.create.await_count, 1)
        await self.bot.on_message(self.message("<@99> " + "字" * 2001, user=2))
        self.assertEqual(self.api.responses.create.await_count, 1)

    async def test_error_releases_capacity_without_storing_history(self):
        self.api.responses.create.side_effect = RuntimeError("secret must not be sent")
        msg = self.message()
        await self.bot.on_message(msg)
        self.assertEqual(self.bot.active, 0)
        self.assertFalse(self.bot.busy)
        self.assertFalse(self.bot.sessions)
        self.assertNotIn("secret", msg.reply.call_args.args[0])

    async def test_expired_history_and_bounded_output(self):
        self.bot.sessions[(10, 20, 1)] = (-999999, [{"role": "user", "content": "old"}])
        self.api.responses.create.return_value.output_text = "哈" * 3000
        msg = self.message()
        await self.bot.on_message(msg)
        self.assertEqual(len(self.api.responses.create.call_args.kwargs["input"]), 1)
        self.assertLessEqual(len(msg.reply.call_args.args[0]), 2000)


if __name__ == "__main__":
    unittest.main()
