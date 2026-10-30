import asyncio,tempfile,unittest
from datetime import datetime,timezone,timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock,patch
from bot import GirlBot
from settings import Settings
from state import State
from providers import QuotaError
from topic_context import resolve_reference,validate_decision

def decision(action='standalone',ids=(),focus=()):
    ids=list(map(str,ids))
    return dict(action=action,topic_start_id=ids[0] if ids else '',relevant_ids=ids,focus_ids=list(map(str,focus or ids[-1:])))

class TopicTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.provider=NS(web_search='live',structured=AsyncMock(return_value=decision()),complete=AsyncMock(return_value=('reply','针对键盘继续聊。')))
        self.bot=GirlBot(self.provider,Settings({20},observed={20},mode='social',debounce_seconds=0),'',State(Path(self.tmp.name)/'state.db'))
        self.bot._connection.user=NS(id=99)
        self.permissions=NS(view_channel=True,read_message_history=True)
        self.rows=[]
        self.reads=[]
        self.channel=NS(id=20,permissions_for=lambda _:self.permissions,fetch_message=AsyncMock())
        async def history(limit,before,oldest_first):
            self.reads.append((limit,before.id))
            for row in [r for r in self.rows if r.id<before.id][:limit]: yield row
        self.channel.history=history
        self.message=self.make(1000,1,'那还值得买吗？')

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    def make(self,mid,user,text,bot=False):
        return NS(id=mid,author=NS(id=user,display_name='合成群友',bot=bot),content=text,
                  channel=self.channel,guild=NS(id=10,me=NS()),reference=None,webhook_id=None,
                  attachments=[],created_at=datetime.now(timezone.utc)-timedelta(hours=2)+timedelta(seconds=mid),
                  reply=AsyncMock(return_value=NS(id=1001)))

    async def test_implicit_followup_reads_back_to_semantic_start_across_authors(self):
        self.rows=[self.make(i,2+i%2,f'话题片段{i}') for i in range(40,0,-1)]
        self.rows[33].content='想买一把键盘，80HE和另一个型号怎么选？'
        self.provider.structured.side_effect=[decision('more'),decision('more'),decision('ready',[7,21,35,40])]
        result=await resolve_reference(self.bot,self.message,self.message.content)
        self.assertEqual(result['topic_start_id'],'7')
        self.assertEqual([m['message_id'] for m in result['messages']],['7','21','35','40'])
        self.assertEqual(self.reads,[(15,1000),(15,26),(15,11)])
        self.assertEqual(result['scanned'],40)
        self.assertEqual(self.provider.web_search,'live')

    async def test_selected_topic_replaces_old_video_in_reply_payload(self):
        self.message.content='<@99> 那还值得买吗？'
        self.rows=[self.make(3,99,'旧泡面视频',True),self.make(2,3,'先看测评'),self.make(1,2,'纠结买80HE')]
        self.provider.structured.return_value=decision('ready',[1,2])
        self.bot.state.save_exchange('10:20',1,'之前','泡面视频')
        await self.bot.on_message(self.message)
        payload=self.provider.complete.call_args.args[1]
        self.assertEqual(payload['reference_context']['focus_ids'],['2'])
        self.assertEqual(payload['history'],[])
        self.assertEqual(payload['discoveries'],[])
        self.assertEqual(payload['recent'],[])

    async def test_standalone_question_stops_after_first_page(self):
        self.rows=[self.make(i,2,'普通话题') for i in range(50,0,-1)]
        result=await resolve_reference(self.bot,self.message,'你好，帮我解释一下质数')
        self.assertIsNone(result)
        self.assertEqual(len(self.reads),1)
        self.provider.structured.assert_awaited_once()

    async def test_explicit_reply_is_required_in_selected_evidence(self):
        self.message.reference=NS(channel_id=20,message_id=7)
        self.channel.fetch_message.return_value=self.make(7,99,'明确回复的机器人消息',True)
        self.provider.structured.return_value=decision('ready',[7])
        result=await resolve_reference(self.bot,self.message,self.message.content)
        self.assertEqual(result['focus_ids'],['7'])
        self.channel.fetch_message.assert_awaited_once_with(7)

    async def test_cross_channel_reference_never_reads(self):
        self.message.reference=NS(channel_id=21,message_id=7)
        result=await resolve_reference(self.bot,self.message,self.message.content)
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(self.reads,[])
        self.provider.structured.assert_not_awaited()

    async def test_opted_out_sensitive_and_other_bot_sources_are_excluded(self):
        self.bot.state.set_muted('10:20',2,True)
        self.rows=[self.make(4,2,'不记录的消息'),self.make(3,3,'我的密码是12345678'),self.make(2,88,'其他机器人',True),self.make(1,3,'公开话题')]
        self.provider.structured.return_value=decision('ready',[1])
        await resolve_reference(self.bot,self.message,self.message.content)
        self.assertEqual([m['message_id'] for m in self.provider.structured.call_args.args[1]['messages']],['1'])

    async def test_no_permission_still_allows_standalone_question(self):
        self.permissions.read_message_history=False
        self.assertIsNone(await resolve_reference(self.bot,self.message,'你好'))
        self.assertEqual(self.reads,[])
        self.assertFalse(self.provider.structured.call_args.args[1]['history_available'])

    async def test_ambiguous_context_asks_without_final_model_guess(self):
        self.message.content='<@99> 那还值得买吗？'
        self.provider.structured.return_value=decision('unclear')
        await self.bot.on_message(self.message)
        self.provider.complete.assert_not_awaited()
        self.assertIn('没理清',self.message.reply.call_args.args[0])

    async def test_bounded_history_and_age_stop_reading(self):
        self.rows=[self.make(i,2,'承接上文') for i in range(200,0,-1)]
        self.provider.structured.return_value=decision('more')
        result=await resolve_reference(self.bot,self.message,self.message.content)
        self.assertEqual(result['status'],'unavailable')
        self.assertEqual(len(self.reads),8)
        self.reads.clear()
        self.rows[0].created_at=datetime.now(timezone.utc)-timedelta(days=8)
        await resolve_reference(self.bot,self.message,self.message.content)
        self.assertEqual(len(self.reads),1)

    async def test_hallucinated_ids_are_not_used(self):
        self.rows=[self.make(1,2,'原话')]
        self.provider.structured.return_value=decision('ready',[999])
        with self.assertRaises(ValueError):
            await resolve_reference(self.bot,self.message,self.message.content)

    async def test_invalidation_during_analysis_prevents_using_sources(self):
        async def analyze(*args):
            self.bot.invalidate('10:20')
            return decision('ready',[1])
        self.rows=[self.make(1,2,'原话')]
        self.provider.structured.side_effect=analyze
        self.assertEqual((await resolve_reference(self.bot,self.message,self.message.content))['status'],'unavailable')

    async def test_no_history_ingested_into_memory_or_cache(self):
        self.rows=[self.make(1,2,'喜欢红茶')]
        self.provider.structured.return_value=decision('ready',[1])
        await resolve_reference(self.bot,self.message,self.message.content)
        self.assertFalse(self.bot.context)
        self.assertEqual(self.bot.state.facts('10:20',2),[])
        self.assertEqual(self.bot.state.history('10:20',2),[])

    async def test_context_analysis_counts_against_social_limit(self):
        self.bot.settings.social_calls=0
        with self.assertRaises(QuotaError):
            await resolve_reference(self.bot,self.message,self.message.content,social=True)
        self.provider.structured.assert_not_awaited()

    async def test_planner_search_is_disabled(self):
        class Provider:
            web_search='live'
            async def structured(provider,*args):
                self.assertEqual(provider.web_search,'disabled')
                return decision()
        self.bot.provider=Provider()
        await resolve_reference(self.bot,self.message,'你好')
        self.assertEqual(self.bot.provider.web_search,'live')
