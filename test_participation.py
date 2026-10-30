import asyncio
import copy
import unittest
from datetime import timedelta
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import test_interests as fixture
from interests import InterestStore
from social_memory import MemoryEngine
from topic_state import TopicEngine, source_row
from participation import validate_plan, background_context, context_bundle, plan_background, should_join


def bundle():
    return {'topic_context':{'status':'resolved','messages':[], 'topic_states':[
        dict(topic_id='t-keyboard',title='键盘选择',background=['比较两款键盘'],progress=['想看实测'],
             pending=['还没决定选哪款'],agreements=[],participants=['1'],sources=[])]},
        'social_memory':{'preferences':[dict(id=1,owner='1',summary='喜欢安静的键盘')], 'shared_conversations':[]},
        'recent_messages':[{'user_id':'1','text':'还在看键盘实测'}], 'recent_bot':[], 'active_people':['1']}


def plan(action='check_in',**changes):
    raw=dict(action=action,interest_id='',topic_id='t-keyboard',memory_ids=[1],target_user_id='1',
             text='那两款键盘的实测有看出区别了吗？')
    if action=='share': raw.update(interest_id='play',text='')
    if action=='silence': raw.update(interest_id='',topic_id='',memory_ids=[],target_user_id='',text='')
    raw.update(changes)
    return raw


class PlanTests(unittest.TestCase):
    def test_checkin_requires_pending_active_participant_and_real_sources(self):
        for changes in ({'topic_id':'unknown'},{'target_user_id':'2'},{'memory_ids':[99]},
                        {'text':'<@1> hello'},{'text':'https://example.com'}, {'memory_ids':[True]}):
            with self.assertRaises(ValueError):
                validate_plan(plan(**changes),bundle(),[dict(id='play')],NS(name='次要聊天'),[])
        b=bundle();b['topic_context']['topic_states'][0]['pending']=[]
        with self.assertRaises(ValueError): validate_plan(plan(),b,[],NS(name='次要聊天'),[])

    def test_no_checkin_in_video_or_meme_channel_or_already_asked_topic(self):
        for name in ('游戏视频','拾表情包','游戏图片'):
            with self.assertRaises(ValueError): validate_plan(plan(),bundle(),[],NS(name=name),[])
        with self.assertRaises(ValueError): validate_plan(plan(),bundle(),[],NS(name='次要聊天'),['t-keyboard'])

    def test_search_choice_cannot_invent_private_search_terms(self):
        with self.assertRaises(ValueError):
            validate_plan(plan('share',interest_id='搜某人的私事'),bundle(),[dict(id='play')],NS(name='次要聊天'),[])
        self.assertEqual(validate_plan(plan('share'),bundle(),[dict(id='play')],NS(name='次要聊天'),[])['interest_id'],'play')

    def test_bundle_keeps_recent_bot_even_with_resolved_topic(self):
        b=context_bundle({'status':'resolved'},{},[],['已经问过键盘了吗'])
        self.assertEqual(b['recent_bot'],['已经问过键盘了吗'])


class ParticipationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.t=fixture.InterestTests();await self.t.asyncSetUp()
        t=self.t;t.bot.settings.memory_channels={20}
        t.bot.memory=MemoryEngine(t.bot);t.bot.topics=TopicEngine(t.bot)
        t.channel.name='次要聊天'
        self.context=bundle()
        self.bg=patch('interests.background_context',AsyncMock(return_value=self.context))
        self.bg.start();self.addCleanup(self.bg.stop)
        t.provider.structured.return_value=plan()

    async def asyncTearDown(self):
        await self.t.asyncTearDown()

    async def test_checkin_sends_once_without_search_and_persists_across_restart(self):
        t=self.t;await t.engine.tick()
        t.channel.send.assert_awaited_once()
        t.fetcher.fetch.assert_not_awaited()
        self.assertTrue(t.channel.send.call_args.kwargs['silent'])
        self.assertFalse(t.channel.send.call_args.kwargs['allowed_mentions'].users)
        self.assertEqual(InterestStore(t.state).checked('10:20'),['t-keyboard'])
        self.assertTrue(t.engine.store.status('10:20')['awaiting'])
        t.engine.store.defer('10:20',0)
        await t.engine.tick()
        t.channel.send.assert_awaited_once()

    async def test_failed_send_remains_uncertain_and_does_not_retry(self):
        t=self.t;t.channel.send.side_effect=RuntimeError('lost response')
        await t.engine.tick()
        self.assertEqual(t.engine.store.checked('10:20'),['t-keyboard'])
        with t.state.db() as db:
            self.assertEqual(db.execute('SELECT status FROM participation_actions').fetchone()[0],'uncertain')
        t.bot.backoff_until=0;t.engine.store.defer('10:20',0)
        await t.engine.tick();t.channel.send.assert_awaited_once()

    async def test_share_planner_selects_public_interest_and_caption_gets_context(self):
        t=self.t;t.channel.name='综合视频'
        t.provider.structured.side_effect=[plan('share'),fixture.decision()]
        await t.engine.tick()
        t.channel.send.assert_awaited_once()
        search=t.provider.structured.call_args.args[1]
        self.assertEqual(search['interest']['id'],'play')
        self.assertNotIn('participation',search)
        self.assertNotIn('安静的键盘',str(search))
        caption=t.provider.complete.call_args.args[1]
        self.assertEqual(caption['participation'],self.context)

    async def test_caption_with_memories_has_no_search_tools(self):
        t=self.t;t.channel.name='综合视频'
        class Provider:
            web_search='live'
            async def structured(inner,rules,payload,schema,**kwargs):
                if 'interest_id' in schema['properties']:
                    self.assertEqual(inner.web_search,'disabled')
                    return plan('share')
                self.assertTrue(kwargs['require_search'])
                self.assertNotIn('participation',payload)
                return fixture.decision()
            async def complete(inner,rules,payload):
                self.assertEqual(inner.web_search,'disabled')
                self.assertEqual(payload['participation'],self.context)
                return 'reply','刚才那个游戏机制，这个解释挺直观。'
        t.bot.provider=Provider()
        await t.engine.tick()
        t.channel.send.assert_awaited_once()
        self.assertEqual(t.bot.provider.web_search,'live')

    async def test_new_message_during_planning_discards_action(self):
        t=self.t
        async def planner(*args,**kwargs):
            t.engine.store.human('10:20',t.bot.now().timestamp())
            return plan()
        t.provider.structured.side_effect=planner
        await t.engine.tick();t.channel.send.assert_not_awaited()
        self.assertEqual(t.engine.store.checked('10:20'),[])

    async def test_privacy_change_during_plan_cancels_without_send(self):
        t=self.t
        async def planner(*args,**kwargs):
            t.bot.invalidate('10:20')
            return plan()
        t.provider.structured.side_effect=planner
        await t.engine.tick();t.channel.send.assert_not_awaited()

    async def test_pause_and_rename_during_plan_block_send(self):
        t=self.t
        async def planner(*args,**kwargs):
            t.channel.name='公告'
            return plan()
        t.provider.structured.side_effect=planner
        await t.engine.tick();t.channel.send.assert_not_awaited()

    async def test_silence_does_not_mark_topic_as_asked(self):
        t=self.t;t.provider.structured.return_value=plan('silence')
        await t.engine.tick();t.channel.send.assert_not_awaited()
        self.assertEqual(t.engine.store.checked('10:20'),[])

    async def test_closed_conversation_waits_for_new_human_activity(self):
        t=self.t
        await t.bot.on_message(t.message('先不聊了'))
        self.assertTrue(t.engine.store.status('10:20')['awaiting'])
        t.engine.store.human('10:20',t.bot.now().timestamp())
        self.assertFalse(t.engine.store.status('10:20')['awaiting'])

    async def test_clear_removes_checkin_metadata(self):
        t=self.t
        t.engine.store.claim_checkin('10:20',plan(),t.bot.now().timestamp(),0)
        t.state.forget('10:20',1)
        self.assertEqual(t.engine.store.checked('10:20'),[])

    async def test_ask_in_one_channel_paces_other_channels(self):
        t=self.t;await t.engine.tick()
        self.assertEqual(t.engine.store.latest_in_guild('10:21'),t.bot.now().timestamp())
        self.assertEqual(t.engine.store.checked('10:21'),[])

    async def test_no_verified_background_skips_all_proactive_actions(self):
        t=self.t
        with patch('interests.background_context',AsyncMock(return_value=None)):
            await t.engine.tick()
        t.provider.structured.assert_not_awaited();t.channel.send.assert_not_awaited()

    async def test_private_planner_has_search_disabled(self):
        t=self.t
        class Provider:
            web_search='live'
            async def structured(inner,*args,**kwargs):
                self.assertEqual(inner.web_search,'disabled')
                return plan()
        t.bot.provider=Provider()
        result=await plan_background(t.bot,t.channel,self.context,t.profile['interests'],[])
        self.assertEqual(result['action'],'check_in')
        self.assertEqual(t.bot.provider.web_search,'live')

    async def test_other_addressee_blocks_even_when_model_requests_reply(self):
        t=self.t;t.provider.structured.return_value={'addressee':'other','action':'reply'}
        payload=dict(route='followup',text='Ryan，你先选角色',speaker={'id':'1'},participation=bundle())
        self.assertFalse(await should_join(t.bot,payload))
        t.provider.structured.return_value={'addressee':'bot','action':'reply'}
        self.assertTrue(await should_join(t.bot,payload))

    async def test_explicit_call_bypasses_optional_gate_failure(self):
        t=self.t;t.provider.structured.side_effect=RuntimeError('test failure')
        payload=dict(route='named',text='大白龙你觉得呢',speaker={'id':'1'},participation=bundle())
        self.assertTrue(await should_join(t.bot,payload))
        t.provider.structured.assert_not_awaited()
        payload['route']='followup'
        self.assertFalse(await should_join(t.bot,payload))

    async def test_turn_gate_prevents_final_generation_in_bot(self):
        t=self.t;t.provider.structured.return_value={'addressee':'other','action':'reply'}
        t.bot.router.touch('10:20',1)
        with patch('bot.resolve_reference',AsyncMock(return_value=None)),patch('bot.relevant_memories',AsyncMock(return_value={})):
            await t.bot.on_message(t.message('Ryan，你先选角色'))
            await asyncio.gather(*list(t.bot.pending.values()))
        t.provider.complete.assert_not_awaited()

    async def test_missing_topic_cannot_trigger_clarification_to_someone_else(self):
        t=self.t;t.provider.structured.return_value={'addressee':'other','action':'silence'}
        t.bot.router.touch('10:20',1)
        msg=t.message('Ryan，那款你选好了吗')
        with patch('bot.resolve_reference',AsyncMock(return_value={'status':'unavailable'})):
            await t.bot.on_message(msg)
            await asyncio.gather(*list(t.bot.pending.values()))
        msg.reply.assert_not_awaited()

    async def test_history_permissions_required_and_closed_sources_filtered(self):
        t=self.t
        self.assertIsNone(await background_context(t.bot,t.channel))
        t.permissions.read_message_history=True
        recent=t.message('还在选安静的键盘');recent.created_at=t.bot.now()-timedelta(minutes=3)
        recent.attachments=[]
        async def history(**kwargs):
            yield recent
        t.channel.history=history
        t.bot.topics.store.verified=AsyncMock(return_value=([],{}))
        with patch('participation.relevant_memories',AsyncMock(return_value={'preferences':[],'shared_conversations':[]})):
            result=await background_context(t.bot,t.channel)
            self.assertEqual(result['active_people'],['1'])
            t.state.set_muted('10:20',1,True)
            self.assertIsNone(await background_context(t.bot,t.channel))

    async def test_idle_history_older_than_day_does_not_wake_people(self):
        t=self.t;t.permissions.read_message_history=True
        old=t.message();old.created_at=t.bot.now()-timedelta(days=2);old.attachments=[]
        async def history(**kwargs):yield old
        t.channel.history=history
        self.assertIsNone(await background_context(t.bot,t.channel))
