import asyncio,tempfile,unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from bot import GirlBot
from settings import Settings
from state import State
from message_reference import resolve_reference

class ReferenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.provider=NS(complete=AsyncMock(return_value=('reply','你指上面推荐80he的那句吧。')))
        self.bot=GirlBot(self.provider,Settings({20},observed={20},mode='social',debounce_seconds=0),'',State(Path(self.tmp.name)/'state.db'))
        self.bot._connection.user=NS(id=99)
        self.permissions=NS(view_channel=True,read_message_history=True)
        self.rows=[]
        self.channel=NS(id=20,permissions_for=lambda _:self.permissions,fetch_message=AsyncMock())
        async def history(**kwargs):
            self.history_args=kwargs
            for row in self.rows: yield row
        self.channel.history=history
        self.message=self.make(10,1,'大肥龙，如何评价⬆️')

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    def make(self,mid,user,text,bot=False):
        return NS(id=mid,author=NS(id=user,display_name='测试群友',bot=bot),content=text,
                  channel=self.channel,guild=NS(id=10,me=NS()),reference=None,webhook_id=None,
                  attachments=[],created_at=datetime.now(timezone.utc),reply=AsyncMock(return_value=NS(id=11)))

    async def test_up_arrow_recovers_old_chat_and_excludes_old_video(self):
        target=self.make(1,2,'纠结你就买80he，那我建议你去看测评。')
        target.created_at-=timedelta(minutes=80)
        self.rows=[self.make(3,99,'旧泡面视频',True),self.make(2,1,'大肥龙，如何评价⬆️'),target]
        self.bot.state.save_exchange('10:20',1,'如何评价','旧泡面视频')
        self.bot.interests=NS(current=None,human=lambda _:False,store=NS(recent=lambda *args:[{'status':'sent','title':'旧泡面视频'}]),close=AsyncMock())
        await self.bot.on_message(self.message)
        await asyncio.gather(*list(self.bot.pending.values()))
        p=self.provider.complete.call_args.args[1]
        self.assertEqual(p['reference_context']['message_id'],'1')
        self.assertIn('80he',p['reference_context']['text'])
        self.assertEqual(p['discoveries'],[])
        self.assertEqual(p['history'],[])
        self.assertEqual(self.history_args['limit'],20)
        self.assertIs(self.history_args['before'],self.message)
        self.assertFalse(self.bot.state.facts('10:20',2))

    async def test_consecutive_short_messages_keep_keyboard_context(self):
        first=self.make(1,2,'纠结就买80he')
        middle=self.make(2,2,'建议你看测评')
        last=self.make(3,2,'闹麻了')
        self.rows=[last,middle,first,self.make(0,3,'别人的旧话题')]
        result=await resolve_reference(self.bot,self.message,self.message.content)
        self.assertEqual(result['message_ids'],['1','2','3'])
        self.assertEqual(result['text'],'纠结就买80he\n建议你看测评\n闹麻了')

    async def test_group_does_not_merge_distant_or_other_author_messages(self):
        last=self.make(3,2,'最近这句')
        distant=self.make(2,2,'很久以前')
        distant.created_at-=timedelta(minutes=6)
        for other in (distant,self.make(2,3,'其他人的话')):
            self.rows=[last,other]
            result=await resolve_reference(self.bot,self.message,self.message.content)
            self.assertEqual(result['message_ids'],['3'])

    async def test_explicit_reply_wins_and_fetches_current_text(self):
        self.message.reference=NS(channel_id=20,message_id=7)
        self.channel.fetch_message.return_value=self.make(7,99,'被明确回复的视频',True)
        result=await resolve_reference(self.bot,self.message,self.message.content)
        self.assertEqual(result['selection'],'explicit_reply')
        self.assertEqual(result['message_id'],'7')

    async def test_cross_channel_reference_cannot_read(self):
        self.message.reference=NS(channel_id=21,message_id=7)
        result=await resolve_reference(self.bot,self.message,self.message.content)
        self.assertEqual(result['status'],'unavailable')
        self.channel.fetch_message.assert_not_awaited()

    async def test_opted_out_target_does_not_fall_back_to_older_message(self):
        self.rows=[self.make(2,2,'私人内容'),self.make(1,3,'更旧内容')]
        self.bot.state.set_muted('10:20',2,True)
        result=await resolve_reference(self.bot,self.message,self.message.content)
        self.assertEqual(result['status'],'unavailable')
        self.assertNotIn('text',result)

    async def test_missing_permission_or_old_target_returns_unavailable(self):
        self.permissions.read_message_history=False
        self.assertEqual((await resolve_reference(self.bot,self.message,self.message.content))['status'],'unavailable')
        self.permissions.read_message_history=True
        old=self.make(1,2,'旧消息')
        old.created_at-=timedelta(days=2)
        self.rows=[old]
        self.assertEqual((await resolve_reference(self.bot,self.message,self.message.content))['status'],'unavailable')

    async def test_missing_reference_asks_for_quote_without_model_guess(self):
        await self.bot.on_message(self.message)
        await asyncio.gather(*list(self.bot.pending.values()))
        self.provider.complete.assert_not_awaited()
        self.assertIn('没定位到',self.message.reply.call_args.args[0])

    async def test_ordinary_chat_does_not_read_history(self):
        self.channel.history=lambda **kwargs: (_ for _ in ()).throw(AssertionError('unexpected read'))
        self.assertIsNone(await resolve_reference(self.bot,self.message,'你好'))
