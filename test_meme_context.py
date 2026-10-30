import asyncio
import copy
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, patch

from meme_context import interpret, validate
from test_vision import Scene, picture


def meaning(**kwargs):
    result = dict(kind='meme', tone='ironic', function='self_mockery', emotion=['无奈'],
        common_usage='夸张地表示开心，也可能反话', current_meaning='对刚才失败自嘲',
        confidence='high', target='self', target_user_id='', evidence_ids=['1'],
        uncertainty='', reply_hint='play_along')
    result.update(kwargs)
    return result


class InterpretationTests(unittest.TestCase):
    def setUp(self):
        self.rows=[dict(message_id='1',user_id='1',text='又输了'),
                   dict(message_id='2',user_id='2',text='我今天吃面')]
        self.current=dict(message_id='3',user_id='1',text='[发送了图片附件]')

    def test_valid_meaning_has_only_selected_evidence(self):
        result=validate(meaning(),self.rows,self.current,{})
        self.assertEqual([r['message_id'] for r in result['evidence']],['1'])

    def test_invented_evidence_and_unsupported_person_fail(self):
        for raw in (meaning(evidence_ids=['999']),
                    meaning(target='person',target_user_id='2'),
                    meaning(target='self',target_user_id='1'),
                    meaning(emotion=['x']*4),meaning(confidence='certain')):
            with self.assertRaises(ValueError):validate(raw,self.rows,self.current,{})

    def test_missing_context_cannot_assert_sender_mood(self):
        for ids in ([],['3']):
            result=validate(meaning(evidence_ids=ids),[],self.current,{})
            self.assertEqual(result['confidence'],'low')
            self.assertEqual(result['target'],'unknown')
            self.assertEqual(result['current_meaning'],'')

    def test_ordinary_image_cannot_gain_emotion(self):
        with self.assertRaises(ValueError):validate(meaning(kind='ordinary'),self.rows,self.current,{})

    def test_unknown_use_cannot_be_high_confidence_even_with_a_question(self):
        current={**self.current,'text':'一般怎么用？'}
        result=validate(meaning(function='unclear',evidence_ids=['3']),[],current,{})
        self.assertEqual(result['confidence'],'low')


class MemeFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.scene=Scene(self.tmp.name)
        self.patch=patch('vision.download',AsyncMock(return_value=picture()))
        self.patch.start()
        self.base=self.scene.provider.structured
        self.interpretations=[]
        async def structured(rules,payload,schema,**kwargs):
            if 'common_usage' in schema['properties']:
                self.interpretations.append(copy.deepcopy(payload))
                raw=self.raw(payload) if callable(self.raw) else self.raw
                if isinstance(raw,Exception):raise raw
                return copy.deepcopy(raw)
            return await self.base(rules,payload,schema,**kwargs)
        self.scene.provider.structured=structured
        self.raw=meaning()

    async def asyncTearDown(self):
        await self.scene.bot.close()
        self.patch.stop();self.tmp.cleanup()

    async def test_same_picture_reinterpreted_with_changed_topic_and_reaches_gate(self):
        self.scene.make(1,'我连输三局了')
        self.scene.bot.router.touch('10:20',1)
        await self.scene.send(self.scene.make(3,image=True))
        first=self.scene.provider.answers[-1]['meme_context']
        self.assertEqual(first['function'],'self_mockery')
        self.scene.make(4,'这局终于赢了')
        self.raw=meaning(tone='sincere',function='celebration',emotion=['开心'],evidence_ids=['4'])
        await self.scene.send(self.scene.make(5,image=True))
        self.assertEqual(self.scene.provider.answers[-1]['meme_context']['function'],'celebration')
        self.assertEqual(len(self.interpretations),2)
        gate=[r for r in self.scene.provider.calls if 'addressee' in r['schema']['properties']][-1]
        self.assertEqual(gate['payload']['meme_context']['function'],'celebration')
        self.assertNotIn('meme_context',self.scene.bot.vision.active[('10:20',1)][1][0])

    async def test_failure_retains_visible_facts_and_normal_reply(self):
        self.raw=RuntimeError('synthetic interpretation failure')
        await self.scene.send(self.scene.make(1,'<@99> 图上是什么',image=True))
        payload=self.scene.provider.answers[-1]
        self.assertTrue(payload['visual_context']['images'])
        self.assertEqual(payload['meme_context']['status'],'unavailable')
        self.assertTrue(self.scene.replies)
        self.assertEqual(self.scene.bot.backoff_until,0)

    async def test_no_image_adds_no_interpretation_call(self):
        await self.scene.send(self.scene.make(1,'<@99> 你好'))
        self.assertFalse(self.interpretations)

    async def test_live_context_excludes_optout_sensitive_old_and_other_channel(self):
        self.scene.make(1,'我连输三局了')
        self.scene.make(2,'不该读取的消息',user=2)
        self.scene.bot.state.set_muted('10:20',2,True)
        old=self.scene.make(3,'昨天别的话题')
        old.created_at=datetime.now(timezone.utc)-timedelta(hours=2)
        # Discord history is chronological, so the old message is the oldest ID.
        del self.scene.messages[3];old.id=0;self.scene.messages[0]=old
        await self.scene.send(self.scene.make(4,'<@99> 看图',image=True))
        rows=self.interpretations[-1]['messages']
        self.assertEqual([r['message_id'] for r in rows],['1'])

    async def test_user_correction_is_passed_as_current_evidence(self):
        self.scene.make(1,'刚才那个建议挺有用')
        self.raw=meaning(tone='sincere',function='agreement',evidence_ids=['3'],current_meaning='明确在夸建议')
        await self.scene.send(self.scene.make(3,'<@99> 我是在夸这个建议，不是阴阳',image=True))
        self.assertIn('不是阴阳',self.interpretations[-1]['current']['text'])
        self.assertEqual(self.scene.provider.answers[-1]['meme_context']['tone'],'sincere')

    async def test_invalidation_during_interpretation_discards_reply(self):
        def invalidate(payload):
            self.scene.bot.invalidate('10:20')
            return meaning(evidence_ids=[])
        self.raw=invalidate
        await self.scene.send(self.scene.make(1,'<@99> 看图',image=True))
        self.assertFalse(self.scene.replies)
