import asyncio
from dataclasses import asdict, replace
import json
import tempfile
import threading
import unittest

from core_engine import (BrowserOptions, BrowserRunner, ConditionLimits, ConditionsService,
    JobStore, ModelError, QARequest, Recorder, TestCondition)
from core_engine.evaluation.condition_site import condition_site, response_plan
from core_engine.qa_schema import encode_plan
from qa_fixtures import ScriptedModel, analysis, intake


def options(role='staff', **kwargs):
    return BrowserOptions(test_context_id=role, session_headers={'Authorization': 'Bearer synthetic-' + role},
                          step_timeout_ms=3000, **kwargs)


def request(origin):
    return QARequest('condition-report', 'demo', origin + '/app', None,
        '직원 계정으로 Preview report 버튼 클릭 후 Complete 문구가 표시되면 GET /app/api/preview 응답이 403입니다.',
        'Preview report 클릭 후 Complete 표시. GET /app/api/preview 응답은 200이어야 합니다.', '')


def model_responses(origin):
    interpreted = intake()
    interpreted.update(required_actions=[{'action': 'click', 'value': '', 'source_quote': 'Preview report 버튼 클릭'}],
                       actions_complete=True, completion_signal={'value': 'Complete', 'source_quote': 'Complete 문구'})
    return [interpreted, {'status': 'ready', 'questions': [], 'rationale': '보고된 응답 비교',
                         'plan': encode_plan(response_plan(origin + '/app'))}, analysis()]


async def reset(*_):
    return True


class ConditionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = JobStore(temporary.name)

    async def test_role_comparison_includes_normal_bug_blocked_and_fixed_retest(self):
        with condition_site() as (origin, policy, state, _):
            registry = (TestCondition('admin', '관리자', options('admin')),
                        TestCondition('staff', '직원', options()),
                        TestCondition('anonymous', '세션 없음', BrowserOptions(step_timeout_ms=3000)))
            model = ScriptedModel(model_responses(origin) * 3 + [analysis()])
            service = ConditionsService(model, policy, self.store, registry)
            result = await service.run(request(origin), reset=reset, is_authorized=lambda: True, select=False)
            self.assertEqual(result.report['matched_condition_ids'], ['staff'], result)
            rows = result.report['conditions']
            self.assertEqual([r['verdict'] for r in rows], ['not_reproduced', 'reproduced', 'inconclusive'])
            self.assertEqual(rows[1]['test_export']['status'], 'ready')
            self.assertEqual(rows[1]['diagnosis']['attempts'][1]['ready'], True)
            self.assertIn('CHECK_TEST_ACCOUNT', [a['code'] for a in rows[2]['diagnosis']['next_actions']])
            self.assertEqual(result.report['coverage']['finished'], 3)
            self.assertNotIn('Bearer synthetic-', json.dumps(asdict(result)))
            state['buggy'] = False
            fixed = await service.retest(result.job_id, 'staff', reset=reset, is_authorized=lambda: True)
            self.assertEqual(fixed.report['regression'], 'resolved_in_test', fixed)
            self.assertEqual(fixed.report['plan_sha256'], rows[1]['plan_sha256'])

    async def test_browser_engines_and_delay_preserve_two_attempts(self):
        with condition_site() as (origin, policy, _, _):
            for engine in ('chromium', 'firefox', 'webkit'):
                with self.subTest(engine=engine):
                    opts = options(browser_name=engine, response_delay_ms=80)
                    recorder = Recorder(self.store, self.store.create(), opts)
                    result = await BrowserRunner(policy, options=opts, recorder=recorder).run(
                        response_plan(origin + '/app'), reset=reset, is_authorized=lambda: True)
                    self.assertEqual(result.verdict.value, 'reproduced', result)
                    self.assertTrue(recorder.browser_version)
                    self.assertEqual(len(result.evidence), 2)
                    self.assertTrue(all(e.stage == 'completed' for e in result.evidence))
                    for ev in result.evidence:
                        self.assertEqual([t['stage'] for t in ev.timeline], ['action', 'action', 'readiness', 'outcome', 'completed'])

    async def test_selector_uses_only_registered_ids_and_preserves_unselected(self):
        with condition_site() as (origin, policy, _, _):
            registry = (TestCondition('staff', '직원', options()), TestCondition('admin', '관리자', options('admin')))
            selection = {'status': 'ready', 'candidates': [{'condition_id': 'staff', 'hypothesis': '직원 조건 비교'}], 'questions': []}
            model = ScriptedModel([selection] + model_responses(origin))
            result = await ConditionsService(model, policy, self.store, registry).run(request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.report['coverage']['not_selected_ids'], ['admin'])
            self.assertNotIn('synthetic-', json.dumps(model.calls[0]['context']))

    async def test_shared_budget_keeps_started_child_and_unrun_conditions(self):
        with condition_site() as (origin, policy, _, _):
            registry = tuple(TestCondition(role, role, options(role)) for role in ('staff', 'admin'))
            model = ScriptedModel(model_responses(origin))
            result = await ConditionsService(model, policy, self.store, registry,
                limits=ConditionLimits(max_model_calls=1)).run(request(origin), reset=reset, is_authorized=lambda: True, select=False)
            self.assertEqual(result.status, 'inconclusive')
            self.assertEqual(len(result.report['model_calls']), 1)
            self.assertIsNotNone(result.report['conditions'][0]['job_id'])
            self.assertEqual(result.report['conditions'][1]['status'], 'not_run')

    async def test_cancellation_records_started_child_in_parent(self):
        with condition_site() as (origin, policy, _, _):
            entered = asyncio.Event()
            async def slow_reset(*_):
                entered.set()
                await asyncio.sleep(30)
                return True
            registry = (TestCondition('staff', '직원', options()), TestCondition('admin', '관리자', options('admin')))
            task = asyncio.create_task(ConditionsService(ScriptedModel([intake()]), policy, self.store, registry).run(
                request(origin), reset=slow_reset, is_authorized=lambda: True, select=False))
            await asyncio.wait_for(entered.wait(), 20)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            parents = [json.loads(p.read_text()) for p in self.store.root.glob('*/result.json')
                       if 'condition_matrix' in p.read_text()]
            self.assertEqual(len(parents), 1)
            rows = parents[0]['report']['conditions']
            self.assertEqual(rows[0]['status'], 'cancelled')
            self.assertIsNotNone(rows[0]['job_id'])
            self.assertEqual(rows[1]['status'], 'not_run')

    async def test_rate_limit_stops_remaining_conditions(self):
        with condition_site() as (origin, policy, _, _):
            registry = (TestCondition('staff', '직원', options()), TestCondition('admin', '관리자', options('admin')))
            result = await ConditionsService(ScriptedModel([ModelError('MODEL_RATE_LIMITED')]), policy, self.store, registry).run(
                request(origin), reset=reset, is_authorized=lambda: True, select=False)
            self.assertEqual(result.report['conditions'][1]['status'], 'not_run')
            self.assertEqual(len(result.report['model_calls']), 1)
