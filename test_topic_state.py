import asyncio
import copy
import json
import tempfile
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import discord
from bot import GirlBot
from settings import Settings
from state import State
from topic_context import resolve_reference
from topic_state import TopicStore, TopicEngine, source_row, RETENTION


def snapshot(ids=('1',), tid='', title='键盘选购', **changes):
    return dict(topic_id=tid, title=title, background=['用户1在比较两款键盘'],
                progress=['用户2建议先看测评'], pending=['用户1还没有决定买哪款'],
                agreements=[], source_ids=list(ids), **changes)


class TopicStateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'state.db'
        self.state = State(self.path)
        self.provider = NS(web_search='live', structured=AsyncMock(), complete=AsyncMock(return_value=('reply', '好呀。')))
        cfg = Settings({20, 21}, observed={20, 21}, memory_channels={20, 21}, mode='observe')
        self.bot = GirlBot(self.provider, cfg, '', self.state)
        self.bot._connection.user = NS(id=99)
        self.engine = self.bot.topics
        self.permissions = NS(view_channel=True, read_message_history=True)
        self.source_messages = {}
        async def fetch(mid):
            return self.source_messages[mid]
        self.channel = NS(id=20, permissions_for=lambda _: self.permissions, fetch_message=AsyncMock(side_effect=fetch))
        async def history(**kwargs):
            for row in []:
                yield row
        self.channel.history = history

    async def asyncTearDown(self):
        await self.bot.close()
        self.tmp.cleanup()

    def message(self, mid=1, text='用户1想买键盘，还在比较。', user=1, **changes):
        values = dict(id=mid, content=text, author=NS(id=user, display_name=f'群友{user}', bot=user==99),
                      guild=NS(id=10, me=NS()), channel=self.channel, webhook_id=None,
                      created_at=datetime.now(timezone.utc)-timedelta(days=2), attachments=[], reference=None,
                      reply=AsyncMock(return_value=NS(id=999)))
        values.update(changes)
        result = NS(**values)
        self.source_messages[mid] = result
        return result

    def save(self, scope='10:20', messages=None, item=None):
        messages = messages or [self.message()]
        item = item or snapshot([str(m.id) for m in messages])
        self.engine.store.save(scope, {'topics': [item]}, [source_row(m) for m in messages], self.engine.store.rows(scope))
        return self.engine.store.rows(scope)[0]

    async def test_restart_preserves_state_without_storing_transcript(self):
        msg = self.message(text='唯一的完整原文不会存入话题表')
        topic = self.save(messages=[msg])
        reopened = TopicStore(State(self.path))
        self.assertEqual(reopened.rows('10:20')[0], topic)
        with self.state.db() as db:
            body = db.execute('SELECT body FROM conversation_topics').fetchone()[0]
        self.assertNotIn(msg.content, body)
        self.assertNotIn('text', json.loads(body)['sources'][0])
        self.assertEqual(reopened.rows('10:21'), [])

    async def test_correction_replaces_progress_and_resolves_pending(self):
        old = self.save()
        update = snapshot(['1', '2'], tid=old['topic_id'])
        update['progress'] = ['用户1决定买入门款']
        update['pending'] = []
        msg = self.message(2, '决定了，买入门款。')
        new = self.save(messages=[msg], item=update)
        self.assertEqual(len(self.engine.store.rows('10:20')), 1)
        self.assertEqual(new['pending'], [])
        self.assertEqual(len(new['sources']), 2)

    async def test_two_topics_have_distinct_sources(self):
        a, b = self.message(), self.message(2, '分享泡面视频', 2)
        self.engine.store.save('10:20', {'topics': [snapshot(['1']), snapshot(['2'], title='泡面视频')]},
                               [source_row(a), source_row(b)], [])
        rows = self.engine.store.rows('10:20')
        self.assertEqual(len(rows), 2)
        self.assertEqual({tuple(r['participants']) for r in rows}, {('1',), ('2',)})

    async def test_invented_ids_foreign_topics_and_invalid_batches_are_atomic(self):
        batch = [source_row(self.message())]
        bad_items = [snapshot(['999']), snapshot(tid='not-in-this-channel'), snapshot(['1', '1']), snapshot()]
        bad_items[-1]['pending'] = ['过长'*181]
        for bad in bad_items:
            with self.assertRaises(ValueError):
                self.engine.store.save('10:20', {'topics': [snapshot(), bad]}, batch, [])
            self.assertEqual(self.engine.store.rows('10:20'), [])

    async def test_old_only_update_is_rejected(self):
        old = self.save()
        with self.assertRaises(ValueError):
            self.save(messages=[self.message(2)], item=snapshot(['1'], tid=old['topic_id']))

    async def test_retention_and_clear_preserve_other_channel(self):
        self.save()
        self.save(scope='10:21')
        with self.state.db() as db:
            db.execute('UPDATE conversation_topics SET updated=? WHERE scope=?', (time.time()-RETENTION-1, '10:20'))
        self.assertEqual(self.engine.store.rows('10:20'), [])
        self.assertEqual(len(self.engine.store.rows('10:21')), 1)
        self.save()
        self.state.forget('10:20', 1)
        self.assertEqual(self.engine.store.rows('10:20'), [])
        self.assertEqual(len(self.engine.store.rows('10:21')), 1)

    async def test_retrieval_after_restart_fetches_evidence_and_excludes_unrelated_video(self):
        keyboard = self.save()
        self.save(messages=[self.message(2, '旧泡面视频', 99)], item=snapshot(['2'], title='泡面视频'))
        self.bot.topics = TopicEngine(self.bot)
        self.provider.structured.return_value = dict(action='ready', topic_start_id='1', relevant_ids=['1'], focus_ids=['1'])
        current = self.message(100, '那便宜那款还值得买吗？')
        result = await resolve_reference(self.bot, current, current.content)
        self.assertEqual(result['topic_states'][0]['topic_id'], keyboard['topic_id'])
        self.assertEqual(len(result['topic_states']), 1)
        self.assertEqual(result['messages'][0]['message_id'], '1')
        self.assertEqual(self.channel.fetch_message.await_count, 2)

    async def test_edit_while_offline_discards_saved_snapshot(self):
        self.save()
        self.source_messages[1].content = '实际已经退货'
        topics, rows = await self.engine.store.verified(self.bot, self.message(100), {})
        self.assertEqual((topics, rows), ([], {}))
        self.assertEqual(self.engine.store.rows('10:20'), [])

    async def test_delete_while_offline_discards_saved_snapshot(self):
        self.save()
        self.channel.fetch_message.side_effect = discord.NotFound(NS(status=404, reason='Not found'), 'Gone')
        self.assertEqual(await self.engine.store.verified(self.bot, self.message(100), {}), ([], {}))
        self.assertEqual(self.engine.store.rows('10:20'), [])

    async def test_transient_read_failure_omits_but_does_not_erase_topic(self):
        self.save()
        self.channel.fetch_message.side_effect = discord.Forbidden(NS(status=403, reason='Forbidden'), 'No')
        self.assertEqual(await self.engine.store.verified(self.bot, self.message(100), {}), ([], {}))
        self.assertEqual(len(self.engine.store.rows('10:20')), 1)

    async def test_recent_sources_are_reused_without_extra_fetches(self):
        self.save()
        topics, _ = await self.engine.store.verified(self.bot, self.message(100), {'1': source_row(self.source_messages[1])})
        self.assertEqual(len(topics), 1)
        self.channel.fetch_message.assert_not_awaited()

    async def test_no_permission_never_injects_saved_state(self):
        self.save()
        self.permissions.read_message_history = False
        self.provider.structured.return_value = dict(action='standalone', topic_start_id='', relevant_ids=[], focus_ids=[])
        await resolve_reference(self.bot, self.message(100), '解释一下质数')
        self.assertEqual(self.provider.structured.call_args.args[1]['topic_states'], [])
        self.channel.fetch_message.assert_not_awaited()

    async def test_future_sources_do_not_leak_into_earlier_turn(self):
        self.save(messages=[self.message(200)], item=snapshot(['200']))
        self.assertEqual(await self.engine.store.verified(self.bot, self.message(100), {}), ([], {}))
        self.channel.fetch_message.assert_not_awaited()

    async def test_optout_removes_topic_and_pending_batch(self):
        self.save()
        self.engine.observe(self.message(2))
        await self.bot.command(self.message(100), '不记录我')
        self.assertEqual(self.engine.store.rows('10:20'), [])
        self.assertEqual(self.engine.inbox, {})
        self.engine.observe(self.message(3))
        self.assertEqual(self.engine.inbox, {})

    async def test_raw_edit_invalidates_topic(self):
        self.save()
        await self.bot.on_raw_message_edit(NS(channel_id=20, guild_id=10, message_id=1, data={'content': '修改'}))
        self.assertEqual(self.engine.store.rows('10:20'), [])

    async def test_silent_observation_updates_topics_in_background(self):
        msg = self.message()
        await self.bot.on_message(msg)
        self.provider.complete.assert_not_awaited()
        self.provider.structured.return_value = {'topics': [snapshot()]}
        self.engine.last_added['10:20'] = 0
        await self.engine.tick()
        self.assertEqual(len(self.engine.store.rows('10:20')), 1)
        self.assertEqual(self.engine.inbox, {})
        self.assertEqual(self.engine.provider.web_search, 'disabled')
        self.assertEqual(self.provider.web_search, 'live')

    async def test_interruption_keeps_batch_for_retry(self):
        self.engine.observe(self.message())
        self.engine.last_added['10:20'] = 0
        entered = asyncio.Event()
        async def slow(*args):
            entered.set()
            await asyncio.Event().wait()
        self.provider.structured.side_effect = slow
        task = asyncio.create_task(self.engine.tick())
        await entered.wait()
        self.engine.interrupt()
        await task
        self.assertEqual(len(self.engine.inbox['10:20']), 1)
        self.assertFalse(self.bot.model_lock.locked())
        self.assertFalse(self.bot.model_busy)

    async def test_clear_during_extraction_cannot_resurrect_topic(self):
        self.engine.observe(self.message())
        self.engine.last_added['10:20'] = 0
        async def invalidate(*args):
            self.bot.epochs['10:20'] = 1
            self.engine.store.clear('10:20')
            return {'topics': [snapshot()]}
        self.provider.structured.side_effect = invalidate
        await self.engine.tick()
        self.assertEqual(self.engine.store.rows('10:20'), [])

    async def test_private_messages_never_reach_queue(self):
        self.engine.observe(self.message(text='我的密码是secret123'))
        self.assertEqual(self.engine.inbox, {})

    async def test_pending_conversation_and_disabled_quota_defer_extraction(self):
        self.engine.observe(self.message())
        self.engine.last_added['10:20'] = 0
        self.bot.waiters = 1
        await self.engine.tick()
        self.provider.structured.assert_not_awaited()
        self.bot.waiters = 0
        self.bot.settings.daily_calls = 0
        await self.engine.tick()
        self.provider.structured.assert_not_awaited()
        self.assertEqual(len(self.engine.inbox['10:20']), 1)

    async def test_all_sources_rechecked_not_only_the_first(self):
        a, b = self.message(), self.message(2, '已经买好了', 2)
        self.save(messages=[a, b])
        b.content = '撤回上面的决定'
        self.assertEqual(await self.engine.store.verified(self.bot, self.message(100), {}), ([], {}))

    async def test_new_messages_during_extraction_are_not_dropped(self):
        self.engine.observe(self.message())
        self.engine.last_added['10:20'] = 0
        async def append(*args):
            self.engine.observe(self.message(2, '新的一句'))
            return {'topics': [snapshot()]}
        self.provider.structured.side_effect = append
        await self.engine.tick()
        self.assertEqual([r['message_id'] for r in self.engine.inbox['10:20']], ['2'])

    async def test_old_topic_ranked_before_latest_three_with_live_sources(self):
        old=self.save()
        for i in range(2,8):
            item=snapshot([str(i)],title='泡面做法')
            item.update(background=['讨论泡面'],progress=[],pending=[])
            self.save(messages=[self.message(i,'聊泡面')],item=item)
        self.provider.structured.return_value=dict(queries=['磁轴键盘 选购'],person_ids=[],
            start_date='',end_date='',time_mode='none')
        topics,evidence=await self.engine.store.verified(self.bot,self.message(100,'那把打游戏用的选好了吗？'),{})
        self.assertEqual(topics[0]['topic_id'],old['topic_id'])
        self.assertIn('1',evidence)
        self.assertEqual(self.channel.fetch_message.await_count,3)

    async def test_changed_old_topic_is_rejected_after_hybrid_lookup(self):
        old=self.save()
        for i in range(2,6):
            self.save(messages=[self.message(i,'聊其他事')],item=snapshot([str(i)],title='其他事情'))
        self.source_messages[1].content='已经撤回'
        self.provider.structured.return_value=dict(queries=['键盘选购'],person_ids=[],
            start_date='',end_date='',time_mode='none')
        topics,_=await self.engine.store.verified(self.bot,self.message(100,'键盘选购'),{})
        self.assertNotIn(old['topic_id'],[t['topic_id'] for t in topics])
        self.assertNotIn(old['topic_id'],[t['topic_id'] for t in self.engine.store.rows('10:20')])
