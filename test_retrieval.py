import copy
import time
import unittest
from datetime import datetime
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo

from retrieval import (tokens, named_people, validate_query, fallback_query, rank_rows,
                       within_time, recorded_times, plan_query, people_for, calendar_range)
import test_memory_context as fixtures

TZ=ZoneInfo('America/Toronto')
NOW=datetime(2026,10,30,12,tzinfo=TZ)


def query(**changes):
    return dict(queries=['饮品 红茶'],person_ids=[],start_date='',end_date='',time_mode='none',**changes)


def memory(mid=1,summary='喜欢无糖红茶',owner='1',stamp=None):
    stamp=stamp if stamp is not None else NOW.timestamp()
    return dict(id=mid,summary=summary,topic='饮品',kind='preference',owner=owner,origin='auto',updated=stamp,
                evidence=[dict(user_id=owner,message_id=str(mid),quote=summary,created=stamp)])


class RankingTests(unittest.TestCase):
    def test_calendar_weekday_and_month_use_actual_calendar(self):
        self.assertEqual(calendar_range('Ryan这周一在群里说过什么',NOW),
                         {'start_date':'2026-10-26','end_date':'2026-10-27'})
        self.assertEqual(calendar_range('上周聊过什么',NOW),
                         {'start_date':'2026-10-19','end_date':'2026-10-26'})
        self.assertEqual(calendar_range('上个月聊过什么',NOW),
                         {'start_date':'2026-09-01','end_date':'2026-10-01'})
        self.assertIsNone(calendar_range('上周到昨天',NOW))

    def test_chinese_bigrams_and_english_tokens(self):
        self.assertEqual(tokens('无糖红茶 Apex 80HE'),['无糖','糖红','红茶','apex','80he'])

    def test_older_synonym_match_survives_many_recent_unrelated_records(self):
        old=memory(stamp=NOW.timestamp()-86400*20)
        rows=[memory(i+2,'喜欢机械键盘',stamp=NOW.timestamp()+i) for i in range(300)]+[old]
        ranked=rank_rows(rows,'喝的给我选哪种',query(),1,[],NOW)
        self.assertEqual(ranked[0]['id'],1)
        self.assertEqual(len(ranked),60)

    def test_original_keyword_survives_bad_expansion(self):
        old=memory(summary='喜欢80HE磁轴键盘',stamp=NOW.timestamp()-86400)
        q=query(); q['queries']=['冰淇淋']
        self.assertEqual(rank_rows([memory(2,'喜欢电影'),old],'80HE',q,1,[],NOW)[0]['id'],1)

    def test_person_constraint_overrides_stronger_other_person_match(self):
        q=query(); q['person_ids']=['2']
        rows=rank_rows([memory(),memory(2,'喜欢咖啡','2')],'红茶',q,1,[],NOW)
        self.assertEqual([r['owner'] for r in rows],['2'])

    def test_ambiguous_name_is_not_assigned_to_either_person(self):
        people={'1':['Ryan'],'2':['Ryan'],'3':['Other']}
        q=fallback_query('Ryan喜欢什么？ <@3>',people)
        self.assertEqual(rank_rows([memory()], 'Ryan',q,1,[],NOW),[])
        self.assertEqual(named_people('Ryan <@2>',people),({'2'},False))

    def test_ascii_name_does_not_match_inside_another_name(self):
        self.assertEqual(named_people('Brian喜欢茶',{'1':['Ryan'],'2':['Ian']}), (set(),False))

    def test_local_recorded_date_boundaries_are_half_open(self):
        q=query(); q.update(time_mode='recorded',start_date='2026-10-19',end_date='2026-10-26')
        stamp=lambda day:datetime(2026,10,day,tzinfo=TZ).timestamp()
        self.assertFalse(within_time(memory(stamp=stamp(19)-1),q,TZ))
        self.assertTrue(within_time(memory(stamp=stamp(19)),q,TZ))
        self.assertTrue(within_time(memory(stamp=stamp(26)-1),q,TZ))
        self.assertFalse(within_time(memory(stamp=stamp(26)),q,TZ))

    def test_dst_day_uses_local_midnights(self):
        q=query(); q.update(time_mode='recorded',start_date='2026-11-01',end_date='2026-11-02')
        self.assertTrue(within_time(memory(stamp=datetime(2026,11,1,23,59,tzinfo=TZ).timestamp()),q,TZ))
        self.assertFalse(within_time(memory(stamp=datetime(2026,11,2,tzinfo=TZ).timestamp()),q,TZ))

    def test_event_date_does_not_filter_by_posting_date(self):
        q=query(); q.update(time_mode='event',start_date='2026-10-19',end_date='2026-10-26')
        self.assertTrue(within_time(memory(),q,TZ))

    def test_person_and_date_must_match_the_same_evidence(self):
        row=memory(owner='2')
        row['evidence'].append(dict(user_id='1',message_id='2',quote='讨论过红茶',
                                   created=datetime(2026,10,22,tzinfo=TZ).timestamp()))
        q=query();q.update(person_ids=['2'],time_mode='recorded',start_date='2026-10-19',end_date='2026-10-26')
        self.assertFalse(within_time(row,q,TZ))

    def test_corrected_memory_cannot_masquerade_as_older_version(self):
        row=memory(stamp=NOW.timestamp()-86400*10)
        row.update(origin='corrected',updated=NOW.timestamp())
        row['evidence'].append(dict(user_id='1',message_id='slash:1',quote='现在喝咖啡',created=NOW.timestamp()))
        self.assertEqual(recorded_times(row),[NOW.timestamp()])

    def test_unknown_person_and_invalid_time_ranges_rejected(self):
        for change in ({'person_ids':['999']},{'start_date':'2026-10-19'},
                       {'time_mode':'recorded','start_date':'2026-10-26','end_date':'2026-10-19'},
                       {'queries':['x'*101]}, {'time_mode':'event','start_date':'2026-02-30','end_date':'2026-03-01'}):
            q=query();q.update(change)
            with self.assertRaises(ValueError): validate_query(q,{'1':[]})


class RetrievalIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.t=fixtures.MemoryContextTests()
        await self.t.asyncSetUp()
        self.t.plan_patch.stop()

    async def asyncTearDown(self):
        await self.t.asyncTearDown()

    async def test_full_pipeline_retrieves_beyond_250_and_preserves_source_check(self):
        t=self.t
        target=t.add()
        with t.bot.state.db() as db:
            for i in range(260):
                mid=db.execute('''INSERT INTO social_memories
                    (scope,owner,kind,category,topic,summary,status,origin,created,updated,expires)
                    VALUES ('10:20','1','preference','hobby',?,'喜欢科幻电影','active','auto',?,?,?)''',
                    (f'电影{i}',time.time(),time.time()+i+1,time.time()+86400)).lastrowid
                db.execute('INSERT INTO memory_evidence VALUES (?,?,?,?,?)',(mid,str(i+200),'1','喜欢科幻电影',time.time()))
        self.assertNotIn(target,[r['id'] for r in t.bot.memory.store.rows('10:20',time.time())])
        t.message.content='给我选个喝的？'
        t.provider.structured.side_effect=[query(),{'memory_ids':[target]},{'memory_ids':[target]}]
        result=await t.recall(False)
        self.assertEqual(result['preferences'][0]['id'],target)
        selected=t.provider.structured.call_args_list[1].args[1]['candidates']
        self.assertEqual(selected[0]['id'],target)
        self.assertEqual(len(selected),60)
        t.channel.fetch_message.assert_awaited_once_with(1)
        self.assertTrue(t.bot.memory.store.manage('10:20',1,target,'delete',time.time()))

    async def test_cached_name_finds_peer_without_recent_message_or_mention(self):
        t=self.t; target=t.add(owner=2)
        t.bot.memory.store.observe_person('10:20',2,['Ryan'])
        t.message.content='Ryan适合点哪杯？'
        t.provider.structured.side_effect=[query(),{'memory_ids':[target]},{'memory_ids':[target]}]
        result=await t.recall(False)
        self.assertEqual(result['preferences'][0]['owner'],'2')
        self.assertEqual(t.provider.structured.call_args.args[1]['retrieval_query']['person_ids'],['2'])

    async def test_unknown_query_plan_falls_back_without_breaking_verification(self):
        t=self.t; target=t.add()
        t.provider.structured.side_effect=[RuntimeError('offline'),{'memory_ids':[target]},{'memory_ids':[target]}]
        self.assertEqual((await t.recall())['preferences'][0]['id'],target)

    async def test_empty_date_window_never_fetches_irrelevant_source(self):
        t=self.t; t.add()
        q=query();q.update(time_mode='recorded',start_date='2000-01-01',end_date='2000-01-02')
        t.provider.structured.return_value=q
        self.assertFalse(any((await t.recall()).values()))
        t.channel.fetch_message.assert_not_awaited()

    async def test_calendar_restricts_incorrect_model_week_and_fallback(self):
        t=self.t;t.add();t.bot.now=lambda:NOW
        t.message.content='Ryan这周一在群里说过喜欢什么？'
        q=query();q.update(time_mode='recorded',start_date='2026-10-26',end_date='2026-11-02')
        t.provider.structured.return_value=q
        planned=await plan_query(t.bot,t.message,t.message.content,{},[])
        self.assertEqual(planned['end_date'],'2026-10-27')
        t.provider.structured.side_effect=RuntimeError('offline')
        fallback=await plan_query(t.bot,t.message,t.message.content,{},[])
        self.assertEqual(fallback['time_mode'],'recorded')
        self.assertEqual(fallback['end_date'],'2026-10-27')

    async def test_forget_during_query_plan_discards_candidates(self):
        t=self.t;t.add()
        async def forget(*args):
            t.bot.invalidate('10:20');t.bot.state.forget('10:20',1)
            return query()
        t.provider.structured.side_effect=forget
        self.assertFalse(any((await t.recall()).values()))
        t.channel.fetch_message.assert_not_awaited()

    async def test_identity_aliases_are_scoped_bounded_and_forgettable(self):
        store=self.t.bot.memory.store
        for i in range(8): store.observe_person('10:20',1,[f'name{i}'])
        self.assertEqual(len(store.people('10:20',{'1'})['1']),5)
        self.assertEqual(store.people('10:21',{'1'}),{})
        self.t.bot.state.forget('10:20',1)
        self.assertEqual(store.people('10:20',{'1'}),{})
        self.t.bot.state.set_muted('10:20',1,True)
        store.observe_person('10:20',1,['ignored'])
        self.assertEqual(store.people('10:20',{'1'}),{})

    async def test_planner_counts_usage_and_disables_search(self):
        t=self.t;t.add()
        class Provider:
            web_search='live'
            async def structured(inner,*args):
                self.assertEqual(inner.web_search,'disabled')
                return query()
        t.bot.provider=Provider()
        await plan_query(t.bot,t.message,'饮品',{},t.bot.memory.store.rows('10:20',time.time()))
        self.assertEqual(t.bot.provider.web_search,'live')
        with t.bot.state.db() as db:
            self.assertEqual(db.execute('SELECT total FROM quota').fetchone()[0],1)
