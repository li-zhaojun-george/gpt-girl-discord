import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from bot import GirlBot
from settings import Settings
from state import State
from social_memory import validate
from memory_context import relevant_memories, candidates_for, select_ids


class MemoryContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Stage-two tests isolate selection and source verification from query planning.
        from retrieval import fallback_query
        self.plan_patch = patch('memory_context.plan_query',AsyncMock(
            side_effect=lambda bot,message,text,*a,**kw: fallback_query(text,{})))
        self.plan_patch.start()
        self.addCleanup(self.plan_patch.stop)
        self.tmp = tempfile.TemporaryDirectory()
        self.provider = NS(web_search='live', structured=AsyncMock(), complete=AsyncMock(return_value=('reply', '选不加糖的红茶吧。')))
        self.bot = GirlBot(self.provider, Settings({20,21}, observed={20,21}, memory_channels={20,21}, mode='social'), '', State(Path(self.tmp.name)/'state.db'))
        self.bot._connection.user = NS(id=99)
        self.permissions = NS(view_channel=True, read_message_history=True)
        self.sources = {}
        async def fetch(mid):
            return self.sources[mid]
        self.channel = NS(id=20, permissions_for=lambda _: self.permissions, fetch_message=AsyncMock(side_effect=fetch))
        self.message = NS(id=1000, channel=self.channel, guild=NS(id=10,me=NS()), author=NS(id=1,display_name='群友1',bot=False),
                          content='那给我选哪杯？', reply=AsyncMock(return_value=NS(id=1001)))
        self.context = {'status':'resolved', 'messages':[{'message_id':'100','user_id':'1','text':'在红茶和咖啡中选一杯'}],
                        'topic_states':[], 'focus_ids':['100']}

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    def add(self, mid=1, owner=1, text='我一直喜欢无糖红茶，不喜欢咖啡。', scope='10:20', certainty='explicit', kind='preference', second=None):
        rows=[]
        for sid, uid, content in [(mid,owner,text)]+([second] if second else []):
            self.sources[sid] = NS(id=sid, channel=self.channel, author=NS(id=uid,display_name=f'群友{uid}',bot=False), content=content, webhook_id=None)
            rows.append(dict(message_id=str(sid), user_id=str(uid), text=content, bot=False, time=time.time()))
        item = dict(kind=kind, category='food', owner=str(owner) if kind=='preference' else '', key='饮品口味' if kind=='preference' else '饮茶经历',
                    summary=text if kind=='preference' else '群友1和群友2在频道讨论过无糖红茶', certainty=certainty,
                    evidence=[{'message_id':r['message_id'],'quote':r['text']} for r in rows])
        items=validate({'memories':[item]}, {r['message_id']:r for r in rows}, {r['message_id'] for r in rows})
        self.bot.memory.store.save(scope, items, time.time())
        return self.bot.memory.store.rows(scope,time.time())[0]['id']

    async def recall(self, context=True, route=None):
        return await relevant_memories(self.bot,self.message,self.message.content,self.context if context else None,[],route)

    def choose(self, *ids):
        self.provider.structured.return_value={'memory_ids':list(ids)}

    async def test_current_topic_keeps_relevant_preference_in_final_reply(self):
        mid=self.add()
        self.choose(mid)
        with patch('bot.resolve_reference', AsyncMock(return_value=self.context)):
            await self.bot.generate(self.message,self.message.content,True)
        payload=self.provider.complete.call_args.args[1]
        self.assertEqual(payload['social_memory']['preferences'][0]['owner'],'1')
        self.assertEqual(payload['reference_context'],self.context)
        self.assertEqual(payload['discoveries'],[])
        self.assertEqual(self.provider.structured.await_count,2)

    async def test_independent_question_can_select_no_memory(self):
        self.add()
        self.message.content='什么是质数？'
        self.choose()
        self.assertEqual((await self.recall(False))['preferences'],[])
        self.channel.fetch_message.assert_not_awaited()
        self.assertEqual(self.provider.structured.await_count,1)

    async def test_candidate_other_channel_and_unrelated_person_excluded(self):
        self.add(scope='10:21')
        self.add(mid=2, owner=2)
        self.add(mid=3,owner=1,certainty='uncertain')
        self.assertEqual(candidates_for(self.bot,'10:20',1,[]),[])
        self.assertEqual((await self.recall())['preferences'],[])
        self.provider.structured.assert_not_awaited()

    async def test_explicit_peer_keeps_correct_owner(self):
        mid=self.add(owner=2)
        self.choose(mid)
        result=await self.recall(route=NS(mentioned_users=(2,)))
        self.assertEqual(result['preferences'][0]['owner'],'2')
        self.assertEqual(result['preferences'][0]['live_evidence'][0]['speaker_id'],'2')

    async def test_episode_preserves_actual_participants_and_both_sources(self):
        mid=self.add(text='刚才我们聊了不加糖的红茶。',kind='episode',second=(2,2,'对，我们刚才聊的是无糖红茶。'))
        self.choose(mid)
        row=(await self.recall())['shared_conversations'][0]
        self.assertEqual(row['participants'],['1','2'])
        self.assertEqual(len(row['live_evidence']),2)

    async def test_changed_quote_is_not_used(self):
        mid=self.add()
        self.sources[1].content='我现在只喝咖啡了。'
        self.choose(mid)
        self.assertEqual((await self.recall())['preferences'],[])
        self.assertEqual(self.provider.structured.await_count,1)

    async def test_unchanged_quote_in_changed_meaning_gets_semantic_recheck(self):
        mid=self.add()
        self.sources[1].content='刚才说“我一直喜欢无糖红茶，不喜欢咖啡。”是在开玩笑，实际相反。'
        self.provider.structured.side_effect=[{'memory_ids':[mid]},{'memory_ids':[]}]
        self.assertEqual((await self.recall())['preferences'],[])
        verify=self.provider.structured.call_args.args[1]
        self.assertEqual(verify['phase'],'verify')
        self.assertIn('开玩笑',verify['candidates'][0]['live_evidence'][0]['text'])

    async def test_slash_correction_supersedes_old_preference_without_public_fetch(self):
        mid=self.add()
        self.bot.memory.store.manage('10:20',1,mid,'correct',time.time(),'现在喜欢咖啡','slash:123')
        self.choose(mid)
        row=(await self.recall())['preferences'][0]
        self.assertEqual(row['summary'],'现在喜欢咖啡')
        self.assertEqual(row['live_evidence'][0]['verification'],'user_control')
        self.channel.fetch_message.assert_not_awaited()

    async def test_public_correction_uses_current_control_source(self):
        mid=self.add()
        self.sources[2]=NS(id=2,channel=self.channel,author=NS(id=1,display_name='群友1',bot=False),content='更正记忆 1 现在喜欢咖啡',webhook_id=None)
        self.bot.memory.store.manage('10:20',1,mid,'correct',time.time(),'现在喜欢咖啡','2')
        self.choose(mid)
        self.assertEqual((await self.recall())['preferences'][0]['summary'],'现在喜欢咖啡')
        self.channel.fetch_message.assert_awaited_once_with(2)

    async def test_no_history_permission_omits_automatic_memory(self):
        mid=self.add()
        self.choose(mid)
        self.permissions.read_message_history=False
        self.assertEqual((await self.recall())['preferences'],[])
        self.channel.fetch_message.assert_not_awaited()

    async def test_muted_source_excluded_before_selection(self):
        self.add(kind='episode', text='刚才我们聊无糖红茶。',second=(2,2,'我们一起聊的红茶很好。'))
        self.bot.state.set_muted('10:20',2,True)
        self.assertEqual((await self.recall())['shared_conversations'],[])
        self.provider.structured.assert_not_awaited()

    async def test_model_failure_does_not_break_answer(self):
        self.add()
        self.provider.structured.side_effect=RuntimeError('test failure')
        with patch('bot.resolve_reference', AsyncMock(return_value=self.context)):
            await self.bot.generate(self.message,self.message.content,True)
        self.provider.complete.assert_awaited_once()
        self.assertEqual(self.provider.complete.call_args.args[1]['social_memory']['preferences'],[])

    async def test_optional_recall_cannot_spend_reserved_answer_capacity(self):
        self.add()
        self.bot.settings.daily_calls=1
        with patch('bot.resolve_reference', AsyncMock(return_value=self.context)):
            await self.bot.generate(self.message,self.message.content,True)
        self.provider.structured.assert_not_awaited()
        self.provider.complete.assert_awaited_once()

    async def test_hallucinated_and_duplicate_ids_rejected(self):
        for ids in ([88],[1,1],[True],['1'],[1]*7):
            with self.assertRaises(ValueError):
                select_ids({'memory_ids':ids},[{'id':1}])

    async def test_clear_during_source_read_discards_everything(self):
        mid=self.add()
        self.choose(mid)
        async def fetch(_):
            self.bot.invalidate('10:20')
            self.bot.state.forget('10:20',1)
            return self.sources[1]
        self.channel.fetch_message.side_effect=fetch
        self.assertEqual((await self.recall())['preferences'],[])

    async def test_record_changed_during_model_call_not_used(self):
        mid=self.add()
        calls=0
        async def select(*args):
            nonlocal calls
            calls+=1
            if calls==2:
                self.bot.memory.store.manage('10:20',1,mid,'correct',time.time(),'喜欢咖啡','slash:42')
            return {'memory_ids':[mid]}
        self.provider.structured.side_effect=select
        self.assertEqual((await self.recall())['preferences'],[])

    async def test_wrong_source_author_is_not_accepted(self):
        mid=self.add()
        self.choose(mid)
        self.sources[1].author.id=2
        self.assertEqual((await self.recall())['preferences'],[])

    async def test_manual_facts_and_current_correction_are_passed_to_selector(self):
        self.add()
        self.bot.state.remember('10:20',1,'今天想试试咖啡')
        self.message.content='我今天想换换口味'
        self.choose()
        await self.recall()
        payload=self.provider.structured.call_args.args[1]
        self.assertEqual(payload['facts'],['今天想试试咖啡'])
        self.assertEqual(payload['current']['text'],self.message.content)

    async def test_selection_has_no_search_and_counts_both_passes(self):
        mid=self.add()
        outer=self
        class Provider:
            web_search='live'
            async def structured(self,*args):
                outer.assertEqual(self.web_search,'disabled')
                return {'memory_ids':[mid]}
        self.bot.provider=Provider()
        await self.recall()
        self.assertEqual(self.bot.provider.web_search,'live')
        with self.bot.state.db() as db:
            self.assertEqual(db.execute('SELECT total FROM quota').fetchone()[0],2)
