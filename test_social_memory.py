import asyncio
import copy
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from bot import GirlBot
from settings import Settings
from social_memory import MemoryEngine, MemoryStore, validate, contains_private
from state import State

def source(mid='1',user='11',text='我一直喜欢乌龙茶，不喜欢咖啡。',stamp=None):
    return {'message_id':mid,'user_id':user,'text':text,'bot':False,'time':stamp or time.time()}

def entry(s=None,**changes):
    s = s or source()
    return {'kind':'preference','category':'food','owner':s['user_id'],'key':'饮品口味',
            'summary':'喜欢乌龙茶，不喜欢咖啡','certainty':'explicit',
            'evidence':[{'message_id':s['message_id'],'quote':s['text']}],**changes}

class MemoryValidationTests(unittest.TestCase):
    def test_discord_identity_is_not_confused_with_phone_number(self):
        self.assertFalse(contains_private('刚才和<@123456789012345678>一起起了龙王面这个梗'))
        self.assertTrue(contains_private('我的手机号是12345678901'))

    def test_owner_source_and_quote_must_match(self):
        s = source()
        for item in (entry(s,owner='22'),entry(s,evidence=[{'message_id':'fake','quote':s['text']}]),
                     entry(s,evidence=[{'message_id':'1','quote':'我最喜欢喝冰可乐。'}])):
            self.assertEqual(validate({'memories':[item]},{'1':s},{'1'}),[])
        self.assertEqual(len(validate({'memories':[entry(s)]},{'1':s},{'1'})),1)

    def test_third_party_sensitive_or_old_only_is_rejected(self):
        for text in ('小李最喜欢喝咖啡了。','我喜欢吃的药是治抑郁的。','我喜欢用密码12345678。'):
            s = source(text=text)
            self.assertEqual(validate({'memories':[entry(s)]},{'1':s},{'1'}),[])
        s = source()
        self.assertEqual(validate({'memories':[entry(s)]},{'1':s},set()),[])

    def test_joke_is_candidate_and_single_person_episode_is_uncertain(self):
        s = source(text='我喜欢咖啡？开玩笑，我更喜欢茶。')
        result = validate({'memories':[entry(s)]},{'1':s},{'1'})
        self.assertEqual(result[0]['certainty'],'uncertain')
        e = source(text='昨晚我们组队赢了一场游戏，太开心了。')
        result = validate({'memories':[entry(e,kind='episode',owner='')]},{'1':e},{'1'})
        self.assertEqual(result[0]['certainty'],'uncertain')

class MemoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = Path(self.folder.name)/'state.sqlite3'
        self.state = State(self.path)
        self.store = MemoryStore(self.state)
        self.now = time.time()

    def tearDown(self):
        self.folder.cleanup()

    def save(self,s=None,scope='10:20',**kwargs):
        s = s or source()
        items = validate({'memories':[entry(s,**kwargs)]},{s['message_id']:s},{s['message_id']})
        return self.store.save(scope,items,self.now)

    def test_restart_identity_scope_and_candidate_isolation(self):
        self.save()
        self.save(source('2','22','我喜欢咖啡，不喜欢茶。'),summary='喜欢咖啡，不喜欢茶')
        other = MemoryStore(State(self.path))
        self.assertEqual(other.retrieve('10:20',11,[],self.now)['preferences'][0]['owner'],'11')
        self.assertEqual(other.retrieve('10:21',11,[],self.now)['preferences'],[])
        self.save(source('3','33'),certainty='uncertain')
        self.assertEqual(other.retrieve('10:20',33,[],self.now)['preferences'],[])

    def test_new_preference_replaces_old_but_older_or_guess_cannot(self):
        self.save(source(stamp=self.now))
        self.save(source('2',text='我现在喜欢咖啡，不喜欢茶。',stamp=self.now+1),summary='现在喜欢咖啡')
        self.save(source('3',stamp=self.now-1),summary='旧说法')
        self.save(source('4',stamp=self.now+2),summary='猜测',certainty='uncertain')
        rows = self.store.rows('10:20',self.now)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['summary'],'现在喜欢咖啡')

    def test_correction_ownership_and_source_removal(self):
        self.save()
        memory_id = self.store.rows('10:20',self.now)[0]['id']
        self.assertFalse(self.store.manage('10:20',22,memory_id,'correct',self.now,'其他人的更正','99'))
        self.assertTrue(self.store.manage('10:20',11,memory_id,'correct',self.now,'喜欢热乌龙茶','99'))
        self.save(source('2',stamp=self.now+10),summary='模型想改回去')
        self.assertEqual(self.store.rows('10:20',self.now)[0]['summary'],'喜欢热乌龙茶')
        self.store.remove_source('10:20',['99'])
        self.assertEqual(self.store.rows('10:20',self.now),[])

    def test_optout_and_forget_remove_shared_evidence(self):
        one = source(text='今天我们一起给泡面这个梗起了龙王面的名字。')
        two = source('2','22','对，咱们就把这个泡面梗叫龙王面了。')
        item = entry(one,kind='episode',owner='',summary='群里把泡面梗叫作龙王面',evidence=[
            {'message_id':'1','quote':one['text']},{'message_id':'2','quote':two['text']}])
        items = validate({'memories':[item]},{'1':one,'2':two},{'1','2'})
        self.store.save('10:20',items,self.now)
        self.assertEqual(len(self.store.retrieve('10:20',11,[],self.now)['shared_conversations']),1)
        self.state.forget('10:20',22)
        self.assertEqual(self.store.rows('10:20',self.now),[])
        self.state.set_muted('10:20',22,True)
        self.assertEqual(self.store.save('10:20',items,self.now),0)

    def test_distinct_older_memories_survive_until_expiry(self):
        for i in range(24):
            self.save(source(str(i),stamp=self.now+i),key=f'口味{i}')
        self.assertEqual(len(self.store.rows('10:20',self.now)),24)
        self.store.prune(self.now+91*86400)
        self.assertEqual(self.store.rows('10:20',self.now+91*86400),[])
        with self.state.db() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM memory_evidence').fetchone()[0],0)

class MemoryEngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.provider = NS(web_search='live',structured=AsyncMock(return_value={'memories':[]}),
                           complete=AsyncMock(return_value=('reply','你好')))
        settings = Settings({20},observed={20},mode='observe',memory_channels={20},debounce_seconds=0)
        self.bot = GirlBot(self.provider,settings,'',State(Path(self.folder.name)/'state.sqlite3'))
        self.bot._connection.user = NS(id=99)
        self.bot.settings.memory_delay = 0
        self.engine = self.bot.memory

    async def asyncTearDown(self):
        await self.bot.close()
        self.folder.cleanup()

    def message(self,text='我一直喜欢乌龙茶，不喜欢咖啡。',mid=1,user=11):
        return NS(id=mid,content=text,author=NS(id=user,bot=False,display_name='合成用户'),
                  guild=NS(id=10),channel=NS(id=20),reference=None,webhook_id=None,
                  reply=AsyncMock(return_value=NS(id=100)))

    async def test_observes_without_speaking_then_extracts_with_search_disabled(self):
        msg = self.message()
        await self.bot.on_message(msg)
        self.provider.complete.assert_not_awaited()
        self.assertEqual(self.engine.provider.web_search,'disabled')
        self.assertEqual(self.provider.web_search,'live')
        s = self.engine.context['10:20'][0]
        self.provider.structured.return_value = {'memories':[entry(s)]}
        await self.engine.tick()
        self.assertEqual(len(self.engine.store.rows('10:20',time.time())),1)
        msg.reply.assert_not_awaited()

    async def test_muted_and_sensitive_content_not_queued(self):
        self.bot.state.set_muted('10:20',11,True)
        await self.bot.on_message(self.message())
        await self.bot.on_message(self.message('我喜欢用的密码是12345678',mid=2,user=22))
        await self.engine.tick()
        self.provider.structured.assert_not_awaited()
        self.assertEqual(self.engine.context,{})

    async def test_forget_cancels_inflight_and_cannot_reinsert(self):
        await self.bot.on_message(self.message())
        s = self.engine.context['10:20'][0]
        started = asyncio.Event()
        async def extract(*args,**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                return {'memories':[entry(s)]}  # Even a provider that swallows cancellation cannot resurrect data.
        self.provider.structured.side_effect = extract
        task = asyncio.create_task(self.engine.tick())
        await started.wait()
        await self.bot.on_message(self.message('<@99> 清空记忆',mid=2))
        await task
        self.assertEqual(self.engine.store.rows('10:20',time.time()),[])
        self.assertFalse(self.engine.inbox.get('10:20'))

    async def test_new_direct_message_preempts_extraction_and_keeps_pending_sources(self):
        await self.bot.on_message(self.message())
        started = asyncio.Event()
        async def extract(*args,**kwargs):
            started.set()
            await asyncio.Event().wait()
        self.provider.structured.side_effect = extract
        task = asyncio.create_task(self.engine.tick())
        await started.wait()
        await self.bot.on_message(self.message('<@99> 你好',mid=2))
        await task
        self.assertFalse(self.bot.model_lock.locked())
        self.assertTrue(self.engine.inbox['10:20'])
        self.provider.complete.assert_awaited_once()

    async def test_message_edit_delete_invalidates_saved_source(self):
        s = source()
        self.engine.store.save('10:20',validate({'memories':[entry(s)]},{'1':s},{'1'}),time.time())
        await self.bot.on_raw_message_edit(NS(channel_id=20,guild_id=10,message_id=1,data={'content':'我改了'}))
        self.assertEqual(self.engine.store.rows('10:20',time.time()),[])

    async def test_memory_command_requires_ownership(self):
        s = source()
        self.engine.store.save('10:20',validate({'memories':[entry(s)]},{'1':s},{'1'}),time.time())
        mid = self.engine.store.rows('10:20',time.time())[0]['id']
        await self.bot.on_message(self.message(f'<@99> 忘记记忆 {mid}',mid=2,user=22))
        self.assertEqual(len(self.engine.store.rows('10:20',time.time())),1)
        await self.bot.on_message(self.message(f'<@99> 忘记记忆 {mid}',mid=3,user=11))
        self.assertEqual(self.engine.store.rows('10:20',time.time()),[])

    async def test_notice_once_without_notifications(self):
        channel = NS(id=20,guild=NS(id=10),send=AsyncMock())
        self.bot.get_channel = lambda _:channel
        await self.bot.memory_notice()
        await self.bot.memory_notice()
        channel.send.assert_awaited_once()
        self.assertTrue(channel.send.call_args.kwargs['silent'])

    def enable_second_channel(self):
        self.bot.settings.allowed.add(21)
        self.bot.settings.observed.add(21)
        self.bot.settings.memory_channels.add(21)

    async def test_multi_channel_batches_persist_without_mixing_sources(self):
        self.enable_second_channel()
        one=self.message()
        two=self.message('我一直喜欢黑咖啡，不喜欢茶。',mid=2)
        two.channel=NS(id=21)
        await self.bot.on_message(one)
        await self.bot.on_message(two)
        seen=[]
        async def extract(rules,payload,schema):
            self.assertEqual(len(payload['messages']),1)
            s=payload['messages'][0]
            seen.append(s['message_id'])
            return {'memories':[entry(s,summary=s['text'])]}
        self.provider.structured.side_effect=extract
        await self.engine.tick()
        await self.engine.tick()
        store=MemoryStore(State(Path(self.folder.name)/'state.sqlite3'))
        self.assertIn('乌龙茶',store.rows('10:20',time.time())[0]['summary'])
        self.assertIn('黑咖啡',store.rows('10:21',time.time())[0]['summary'])
        self.assertCountEqual(seen,['1','2'])
        self.assertEqual(store.rows('10:22',time.time()),[])

    async def test_other_channel_evidence_cannot_be_used(self):
        self.enable_second_channel()
        await self.bot.on_message(self.message())
        other=self.message('我喜欢黑咖啡。',mid=2)
        other.channel=NS(id=21)
        await self.bot.on_message(other)
        wrong=self.engine.context['10:21'][0]
        self.provider.structured.return_value={'memories':[entry(wrong)]}
        await self.engine.tick()
        self.assertEqual(self.engine.store.rows('10:20',time.time()),[])

    async def test_oldest_pending_channel_wins_after_busy_channel_refills(self):
        self.enable_second_channel()
        await self.bot.on_message(self.message())
        other=self.message(mid=2)
        other.channel=NS(id=21)
        await self.bot.on_message(other)
        self.provider.structured.return_value={'memories':[]}
        await self.engine.tick()
        await self.bot.on_message(self.message(mid=3))
        await self.engine.tick()
        self.assertEqual(self.provider.structured.call_args.args[1]['new_message_ids'],['2'])

    async def test_optout_is_enforced_in_its_channel(self):
        self.enable_second_channel()
        self.bot.state.set_muted('10:21',11,True)
        await self.bot.on_message(self.message())
        other=self.message(mid=2)
        other.channel=NS(id=21)
        await self.bot.on_message(other)
        self.assertTrue(self.engine.inbox.get('10:20'))
        self.assertFalse(self.engine.inbox.get('10:21'))

    async def test_new_channel_gets_notice_without_repeating_existing_notice(self):
        self.enable_second_channel()
        channels={cid:NS(id=cid,guild=NS(id=10),send=AsyncMock()) for cid in (20,21)}
        self.bot.get_channel=channels.get
        with self.bot.state.db() as db:
            db.execute("INSERT INTO memory_notices VALUES ('10:20','sent')")
        await self.bot.memory_notice()
        await self.bot.memory_notice()
        channels[20].send.assert_not_awaited()
        channels[21].send.assert_awaited_once()

    async def test_idle_source_buffers_expire_without_new_messages(self):
        await self.bot.on_message(self.message())
        self.engine.context['10:20'][0]['time'] = time.time()-1801
        await self.engine.tick()
        self.assertEqual(self.engine.context,{})
        self.assertEqual(self.engine.inbox,{})
        self.provider.structured.assert_not_awaited()
