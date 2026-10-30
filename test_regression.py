import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from regression import load_suite, run_case, assess, ReplayProvider


class FixedConversationTests(unittest.IsolatedAsyncioTestCase):
    """Deterministic transport/storage regressions; language quality uses regression.py --live."""


SUITE=load_suite()
for scenario in SUITE['cases']:
    def create_test(case):
        async def test(self):
            result=await run_case(case,SUITE)
            self.assertTrue(result['passed'],result['errors'])
        return test
    setattr(FixedConversationTests,'test_'+scenario['id'],create_test(scenario))


class EvaluationTests(unittest.TestCase):
    def test_report_rejects_missing_expected_context_and_wrong_action(self):
        case=next(c for c in SUITE['cases'] if c['id']=='switch_keyboard_video')
        result=dict(replies=[],context_ids=['13'],source_fetches=[])
        errors=assess(case,result)
        self.assertIn('reply/silence mismatch',errors)
        self.assertIn('missing expected context',errors)
        self.assertIn('unrelated context selected',errors)

    def test_suite_has_all_four_categories_and_stable_unique_ids(self):
        self.assertEqual(len({c['category'] for c in SUITE['cases']}),4)
        self.assertEqual(len({c['id'] for c in SUITE['cases']}),len(SUITE['cases']))

    def test_valid_coffee_variant_is_not_a_false_failure(self):
        case=next(c for c in SUITE['cases'] if c['id']=='preference_temporary_override')
        result=dict(replies=['今天可以试试拿铁。'],memory_owners=[],stored_sources=['21'],context_ids=[],source_fetches=[])
        self.assertEqual(assess(case,result),[])
        result['replies']=['还是按以前口味喝红茶。']
        self.assertIn('answer lacks expected subject',assess(case,result))


class LiveAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_delegation_does_not_send_oracle_expectations(self):
        live=NS(web_search='live',structured=AsyncMock(return_value={'action':'standalone'}))
        provider=ReplayProvider(SUITE['cases'][0],live)
        payload={'current':{'text':'合成问题'},'messages':[]}
        result=await provider.structured('rules',payload,{'properties':{'topic_start_id':{}}})
        self.assertEqual(result,{'action':'standalone'})
        self.assertEqual(live.structured.call_args.args[1],payload)
        self.assertNotIn('oracle',str(live.structured.call_args))
        self.assertEqual(live.web_search,'live')

    async def test_network_search_not_allowed_in_replay(self):
        provider=ReplayProvider(SUITE['cases'][0])
        with self.assertRaises(ValueError):
            await provider.structured('',{}, {'properties':{}},require_search=True)
