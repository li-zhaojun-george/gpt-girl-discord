import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from bot import GirlBot
from settings import Settings
from social_memory import validate
from state import State

class SlashTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.provider = NS(web_search='live',complete=AsyncMock(),structured=AsyncMock())
        settings = Settings({20},observed={20},mode='social',memory_channels={20},interest_channels={20})
        self.bot = GirlBot(self.provider,settings,'',State(Path(self.folder.name)/'state.sqlite3'))
        self.bot._connection.user = NS(id=99)
        self.tree = self.bot.controls.tree

    async def asyncTearDown(self):
        await self.bot.close()
        self.folder.cleanup()

    def interaction(self,user=11,channel=20,manager=False):
        return NS(id=123,guild=NS(id=10),channel=NS(id=channel),channel_id=channel,
            user=NS(id=user,guild_permissions=NS(manage_guild=manager)),
            response=NS(defer=AsyncMock(),send_message=AsyncMock(),is_done=lambda:True),
            edit_original_response=AsyncMock())

    async def call(self,path,i,*args,**kwargs):
        names = path.split()
        command = self.tree.get_command(names[0])
        for name in names[1:]:
            command = command.get_command(name)
        await command.callback(i,*args,**kwargs)

    async def test_usage_is_private_exact_and_does_not_call_model(self):
        day = self.bot.now().date().isoformat()
        self.bot.state.reserve_call(day,-1,-1,False)
        self.bot.state.reserve_call(day,-1,-1,True)
        i = self.interaction()
        await self.call('用量',i)
        i.response.defer.assert_awaited_once_with(ephemeral=True,thinking=True)
        self.assertIn('模型调用：2 次',i.edit_original_response.call_args.kwargs['content'])
        self.assertIn('主动判断及兴趣探索：1 次',i.edit_original_response.call_args.kwargs['content'])
        self.provider.complete.assert_not_awaited()
        self.provider.structured.assert_not_awaited()

    async def test_outside_channel_and_dm_cannot_read_or_mutate(self):
        i = self.interaction(channel=21)
        await self.call('记忆 保存',i,'喜欢茶')
        i.response.send_message.assert_awaited_once()
        self.assertTrue(i.response.send_message.call_args.kwargs['ephemeral'])
        self.assertEqual(self.bot.state.facts('10:21',11),[])
        i = self.interaction()
        i.guild = None
        await self.call('用量',i)
        i.edit_original_response.assert_not_awaited()

    async def test_reminders_are_owned_and_creation_stays_private(self):
        i = self.interaction()
        await self.call('提醒 创建',i,30,'休息一下')
        row = self.bot.state.reminders('10:20',11)[0]
        self.assertEqual(row['text'],'休息一下')
        await self.call('提醒 取消',self.interaction(user=22),row['id'])
        self.assertEqual(self.bot.state.reminders('10:20',11)[0]['status'],'pending')
        await self.call('提醒 取消',self.interaction(),row['id'])
        self.assertEqual(self.bot.state.reminders('10:20',11)[0]['status'],'cancelled')

    async def test_stop_recording_clears_and_blocks_new_saved_fact(self):
        await self.call('记忆 保存',self.interaction(),'喜欢茶')
        await self.call('记录 停止',self.interaction())
        self.assertTrue(self.bot.state.muted('10:20',11))
        self.assertEqual(self.bot.state.facts('10:20',11),[])
        await self.call('记忆 保存',self.interaction(),'喜欢面')
        self.assertEqual(self.bot.state.facts('10:20',11),[])

    async def test_clear_needs_explicit_true(self):
        self.bot.state.remember('10:20',11,'喜欢茶')
        await self.call('记忆 清空',self.interaction())
        self.assertEqual(self.bot.state.facts('10:20',11),['喜欢茶'])
        await self.call('记忆 清空',self.interaction(),True)
        self.assertEqual(self.bot.state.facts('10:20',11),[])

    async def test_shared_channel_setting_requires_manager(self):
        await self.call('分享 暂停',self.interaction())
        self.assertFalse(self.bot.interests.store.status('10:20')['paused'])
        await self.call('分享 暂停',self.interaction(manager=True))
        self.assertTrue(self.bot.interests.store.status('10:20')['paused'])

    async def test_slash_correction_is_not_forged_public_message_source(self):
        s = {'message_id':'1','user_id':'11','text':'我一直喜欢乌龙茶。','bot':False,'time':time.time()}
        item = {'kind':'preference','category':'food','owner':'11','key':'饮品','summary':'喜欢乌龙茶',
                'certainty':'explicit','evidence':[{'message_id':'1','quote':s['text']}]}
        self.bot.memory.store.save('10:20',validate({'memories':[item]},{'1':s},{'1'}),time.time())
        mid = self.bot.memory.store.rows('10:20',time.time())[0]['id']
        await self.call('记忆 更正',self.interaction(user=22),mid,'喜欢咖啡')
        self.assertEqual(self.bot.memory.store.rows('10:20',time.time())[0]['summary'],'喜欢乌龙茶')
        await self.call('记忆 更正',self.interaction(),mid,'喜欢红茶')
        i = self.interaction()
        await self.call('记忆 来源',i,mid)
        text = i.edit_original_response.call_args.kwargs['content']
        self.assertIn('私密 / 指令',text)
        self.assertNotIn('/20/slash:',text)
        self.assertFalse(self.bot.sent_ids)
        self.assertFalse(self.bot.memory.inbox)

    async def test_sync_is_guild_only_idempotent_and_keeps_unrelated_commands(self):
        self.bot.get_channel = lambda _:NS(guild=NS(id=10))
        self.tree.fetch_commands = AsyncMock(return_value=[NS(name='unrelated',type=discord.AppCommandType.chat_input)])
        self.tree.sync = AsyncMock(return_value=list(self.tree.get_commands()))
        await self.bot.controls.sync_enabled_guilds()
        self.tree.sync.assert_not_awaited()
        self.tree.fetch_commands.return_value = []
        await self.bot.controls.sync_enabled_guilds()
        await self.bot.controls.sync_enabled_guilds()
        self.tree.sync.assert_awaited_once()
        self.assertEqual(self.tree.sync.call_args.kwargs['guild'].id,10)

    def test_command_schema_contains_typed_ranges_and_seven_roots(self):
        self.assertEqual({c.name for c in self.tree.get_commands()},{'用量','帮助','状态','记忆','提醒','分享','记录'})
        data = self.tree.get_command('提醒').to_dict(self.tree)
        create = next(c for c in data['options'] if c['name']=='创建')
        minutes = next(o for o in create['options'] if o['name']=='分钟')
        self.assertEqual((minutes['min_value'],minutes['max_value']),(1,43200))
