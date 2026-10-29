import unittest
import os
from unittest.mock import patch
from settings import Settings
from types import SimpleNamespace as NS
from conversation import ConversationRouter


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.now = 1000
        self.router = ConversationRouter(("GPT娘", "大白龙"), clock=lambda: self.now)

    def route(self, text, user=1, reference=None, observed=True, known=False):
        message = NS(content=text, author=NS(id=user), channel=NS(id=20), reference=reference)
        return self.router.classify(message, 99, "10:20", observed, known).kind

    def test_default_environment_recognizes_fat_dragon_alias(self):
        with patch.dict(os.environ,{'ALLOWED_CHANNEL_IDS':'20'},clear=True):
            settings=Settings.from_env()
        self.router=ConversationRouter(settings.aliases)
        self.assertEqual(self.route('大肥龙，如何评价⬆️'),'named')
        self.assertEqual(self.route('<@123> 大肥龙，如何评价'),'other')

    def test_focus_expiry_and_scope(self):
        self.router.touch("10:20", 1)
        self.assertEqual(self.route("为什么"), "followup")
        self.assertEqual(self.route("为什么", user=2), "ambient")
        self.now += 181
        self.assertEqual(self.route("为什么"), "ambient")

    def test_aliases_and_other_addressees(self):
        self.assertEqual(self.route("gpt娘你怎么看"), "named")
        self.assertEqual(self.route("大白龙？"), "named")
        self.assertEqual(self.route("<@123> 你可以不用@试试看"), "other")
        self.assertEqual(self.route("<@!99> 你认识<@123>吗"), "direct")

    def test_replies_and_mentions_outside_observed_scope(self):
        self.assertEqual(self.route("为什么", observed=False), "none")
        self.assertEqual(self.route("大白龙", observed=False), "none")
        self.assertEqual(self.route("<@99> 为什么", observed=False), "direct")
        reference = NS(resolved=NS(author=NS(id=99), channel=NS(id=20)))
        self.assertEqual(self.route("为什么", reference=reference), "direct")
        self.assertEqual(self.route("为什么", reference=NS(resolved=None), known=True), "direct")
        self.assertEqual(self.route("为什么", reference=NS(resolved=None)), "other")

    def test_cross_channel_reference_not_treated_as_bot_reply(self):
        reference = NS(resolved=NS(author=NS(id=99), channel=NS(id=21)))
        self.assertEqual(self.route("为什么", reference=reference), "other")

    def test_focus_is_bounded_and_clear_is_scoped(self):
        for user in range(1300):
            self.router.touch("10:20", user)
        self.assertEqual(len(self.router.active), 1200)
        self.router.touch("10:21", 1)
        self.router.clear("10:20")
        self.assertEqual(list(self.router.active), [("10:21", 1)])
