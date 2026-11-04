import asyncio
from dataclasses import asdict, replace
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from core_engine import (BrowserOptions, JobStore, ModelError, QALimits, QARequest, QAService)
from fixture_site import site
from qa_fixtures import ScriptedModel, analysis, check, intake, plan, screenshot
from test_transport import local_policy


async def reset(_):
    return True


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'install browser extra')
class QATests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(self.temp.name)

    def request(self, origin, **changes):
        return QARequest('report-1', 'demo', origin + '/app/mixed', screenshot(),
                         'cat 검색 후 missing 검색 시 이전 목록이 남음', '빈 목록 표시',
                         'cat은 결과 있음, missing은 결과 없음', **changes)

    def service(self, model, origin, **kwargs):
        return QAService(model, local_policy(origin), self.store,
                         options=BrowserOptions(step_timeout_ms=150), **kwargs)

    async def test_screenshot_to_reproduced_report_and_retest_fixed(self):
        with site() as (origin, _, state):
            model = ScriptedModel([intake(), plan(origin + '/app/mixed'), analysis(), analysis()])
            service = self.service(model, origin)
            result = await service.run(self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'completed', result)
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
            self.assertEqual([call['images'][0].sha256 for call in model.calls[:1]], [self.request(origin).screenshot.sha256])
            self.assertEqual(model.calls[1]['context']['image_order'], ['current_page', 'reporter_screenshot'])
            self.assertEqual(len(model.calls[1]['images']), 2)
            self.assertEqual(model.calls[1]['images'][1].sha256, self.request(origin).screenshot.sha256)
            self.assertTrue(result.report['observations'])
            self.assertTrue(result.report['human_review_required'])
            self.assertEqual(len(model.calls), 3)
            self.assertIn('QA 검증 보고서', self.store.read(result.job_id, 'report.md').decode())
            self.assertEqual(self.store.read_json(result.job_id, 'result.json')['status'], 'completed')
            self.assertEqual(self.store.read_json(result.job_id, 'events.json')[-1]['kind'], 'completed')
            for ev in result.report['run']['evidence']:
                self.assertTrue(ev['observation_ids'])
                self.assertTrue(ev['symptom_observed'])
                self.assertFalse(ev['expected_observed'])
            state['fixed'] = True
            second = await service.retest(result.job_id, reset=reset, is_authorized=lambda: True)
            self.assertEqual(second.status, 'completed', second)
            self.assertEqual(second.report['run']['verdict']['value'], 'not_reproduced')
            self.assertEqual(second.report['regression'], 'resolved_in_test')
            self.assertEqual(second.report['plan_sha256'], result.report['plan_sha256'])
            self.assertEqual(len(model.calls), 4)  # Retest never regenerates the plan/oracle.

    async def test_needs_info_then_resume_with_answers(self):
        with site() as (origin, hits, _):
            model = ScriptedModel([intake('needs_info'), intake(), plan(origin + '/app/mixed'), analysis()])
            service = self.service(model, origin)
            request = replace(self.request(origin), description='', expected_result='', test_data='')
            result = await service.run(request, reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'needs_info', result)
            self.assertTrue(result.questions)
            self.assertEqual(hits, [])
            continued = await service.resume(result.job_id, 'cat 검색 후 missing 검색, 빈 목록이 정상입니다.',
                                             reset=reset, is_authorized=lambda: True)
            self.assertEqual(continued.status, 'completed', continued)
            self.assertEqual(continued.report['parent_job_id'], result.job_id)

    async def test_locator_repair_keeps_action_values_and_oracles(self):
        with site() as (origin, _, _):
            original = plan(origin + '/app/mixed', broken=True)
            repair = {'status': 'repair', 'target': '#search', 'reason': '동일 검색 버튼의 현재 ID 사용', 'questions': []}
            model = ScriptedModel([intake(), original, repair, analysis()])
            result = await self.service(model, origin).run(self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'completed', result)
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
            self.assertEqual(len(result.report['repairs']), 1)
            scenario = result.report['scenario']
            for key in ('ready', 'expected', 'symptom'):
                self.assertEqual(scenario[key], original['plan'][key])
            self.assertEqual(scenario['steps'][2]['target'], '#search')
            self.assertEqual(self.store.read_json(result.job_id, 'run_0.json')['verdict']['value'], 'inconclusive')

    async def test_model_cannot_repair_oracle_or_invent_selector(self):
        for repair in (
            {'status': 'repair', 'target': '#invented', 'reason': 'fake', 'questions': []},
            {'status': 'repair', 'target': '#search', 'reason': 'fake', 'questions': [], 'expected': check('#result', 'cat')},
        ):
            with site() as (origin, _, _):
                model = ScriptedModel([intake(), plan(origin + '/app/mixed', True), repair])
                result = await self.service(model, origin).run(self.request(origin), reset=reset, is_authorized=lambda: True)
                self.assertEqual(result.error_code, 'MODEL_INVALID_OUTPUT', result)
                self.assertEqual(result.report['run']['verdict']['value'], 'inconclusive')
                self.assertEqual(result.report['repairs'], [])

    async def test_repair_stop_accepts_empty_target_and_preserves_report(self):
        with site() as (origin, _, _):
            repair = {'status': 'stop', 'target': '', 'reason': '동일한 조작 대상을 확인할 수 없음', 'questions': []}
            model = ScriptedModel([intake(), plan(origin + '/app/mixed', True), repair, analysis()])
            result = await self.service(model, origin).run(self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'inconclusive', result)
            self.assertIsNone(result.error_code)
            self.assertIsNotNone(result.report['analysis'])
            self.assertEqual(result.report['repairs'], [])
            self.assertEqual(len(model.calls), 4)

    async def test_repair_needs_info_accepts_empty_target(self):
        with site() as (origin, _, _):
            repair = {'status': 'needs_info', 'target': '', 'reason': '검색 버튼 확인 필요',
                      'questions': ['현재 검색 버튼을 알려주세요.']}
            model = ScriptedModel([intake(), plan(origin + '/app/mixed', True), repair])
            result = await self.service(model, origin).run(self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'needs_info', result)
            self.assertIsNone(result.error_code)
            self.assertEqual(result.questions, tuple(repair['questions']))
            self.assertEqual(result.report['repairs'], [])

    async def test_final_outcome_assertion_is_rejected_before_execution(self):
        with site() as (origin, _, _):
            data = plan(origin + '/app/mixed')
            data['plan']['steps'].append({'action': 'assert_text', 'target': '#result', 'value': 'empty'})
            model = ScriptedModel([intake(), data])
            result = await self.service(model, origin).run(self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.error_code, 'INVALID_PLAN_OR_BASELINE', result)
            self.assertIsNone(result.report['run'])
            self.assertEqual(len(model.calls), 2)
            self.assertEqual([s['phase'] for s in result.report['observations']], ['discovery'])

    async def test_ambiguous_oracle_is_inconclusive_and_not_replanned(self):
        with site() as (origin, _, _):
            data = plan(origin + '/app/mixed')
            data['plan']['expected'] = check('#ready', 'done')
            model = ScriptedModel([intake(), data, analysis()])
            result = await self.service(model, origin).run(self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'inconclusive', result)
            self.assertEqual(result.report['run']['evidence'][0]['blocker'], 'ORACLE_AMBIGUOUS')
            self.assertEqual(result.report['repairs'], [])

    async def test_out_of_scope_model_navigation_never_sent(self):
        with site() as (origin, hits, _):
            data = plan(origin + '/app/mixed')
            data['plan']['steps'].append({'action': 'navigate', 'target': origin + '/outside', 'value': ''})
            result = await self.service(ScriptedModel([intake(), data]), origin).run(
                self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.error_code, 'PATH_OUT_OF_SCOPE', result)
            self.assertFalse(any(path == '/outside' for _, path, _ in hits))

    async def test_summary_failure_preserves_verdict(self):
        with site() as (origin, _, _):
            model = ScriptedModel([intake(), plan(origin + '/app/mixed'), ModelError('MODEL_RATE_LIMITED')])
            result = await self.service(model, origin).run(self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'completed', result)
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
            self.assertIsNone(result.report['analysis'])
            self.assertEqual(result.error_code, 'MODEL_RATE_LIMITED')

    async def test_model_call_limit_blocks_further_paid_calls(self):
        with site() as (origin, _, _):
            model = ScriptedModel([intake()])
            result = await self.service(model, origin, limits=QALimits(max_model_calls=1)).run(
                self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.error_code, 'MODEL_CALL_LIMIT', result)
            self.assertEqual(len(model.calls), 1)

    async def test_revocation_during_model_call_stops_before_browser(self):
        with site() as (origin, hits, _):
            authorized = [True]
            def revoke(_):
                authorized[0] = False
                return intake()
            model = ScriptedModel([revoke])
            result = await self.service(model, origin).run(self.request(origin), reset=reset,
                                                         is_authorized=lambda: authorized[0])
            self.assertEqual(result.error_code, 'AUTHORIZATION_REVOKED', result)
            self.assertEqual(hits, [])

    async def test_cancellation_during_model_call_does_not_start_browser(self):
        with site() as (origin, hits, _):
            cancelled = threading.Event()
            model = ScriptedModel([lambda _: (cancelled.set(), intake())[1]])
            result = await self.service(model, origin).run(self.request(origin), reset=reset,
                                                         is_authorized=lambda: True, cancelled=cancelled)
            self.assertEqual(result.status, 'cancelled', result)
            self.assertEqual(hits, [])

    async def test_cross_program_request_rejected_before_model(self):
        with site() as (origin, hits, _):
            model = ScriptedModel([])
            request = replace(self.request(origin), program_id='other')
            with self.assertRaises(ValueError):
                await self.service(model, origin).run(request, reset=reset, is_authorized=lambda: True)
            self.assertEqual(model.calls, [])
            self.assertEqual(hits, [])

    async def test_reset_failure_returns_explicit_inconclusive(self):
        with site() as (origin, hits, _):
            async def bad_reset(_):
                return False
            result = await self.service(ScriptedModel([intake()]), origin).run(
                self.request(origin), reset=bad_reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'inconclusive', result)
            self.assertEqual(result.error_code, 'RESET_FAILED')
            self.assertEqual(hits, [])

    async def test_tampered_baseline_is_not_executed(self):
        with site() as (origin, hits, _):
            model = ScriptedModel([intake(), plan(origin + '/app/mixed'), analysis()])
            service = self.service(model, origin)
            first = await service.run(self.request(origin), reset=reset, is_authorized=lambda: True)
            saved = self.store.read_json(first.job_id, 'scenario.json')
            saved['plan']['symptom']['value'] = 'different'
            self.store.write_json(first.job_id, 'scenario.json', saved)
            before = len(hits)
            result = await service.retest(first.job_id, reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.error_code, 'INVALID_PLAN_OR_BASELINE', result)
            self.assertEqual(len(hits), before)

    async def test_retest_rejects_changed_viewport_and_test_identity(self):
        with site() as (origin, hits, _):
            model = ScriptedModel([intake(), plan(origin + '/app/mixed'), analysis()])
            service = self.service(model, origin)
            first = await service.run(self.request(origin), reset=reset, is_authorized=lambda: True)
            before = len(hits)
            for options in (BrowserOptions(width=800), BrowserOptions(test_context_id='different-data')):
                changed = QAService(model, local_policy(origin), self.store, options=options)
                result = await changed.retest(first.job_id, reset=reset, is_authorized=lambda: True)
                self.assertEqual(result.error_code, 'BASELINE_ENVIRONMENT_MISMATCH', result)
            self.assertEqual(len(hits), before)
            self.assertEqual(len(model.calls), 3)

    async def test_repair_count_is_bounded_even_when_model_keeps_trying(self):
        with site() as (origin, _, _):
            model = ScriptedModel([intake(), plan(origin + '/app/mixed', True),
                {'status': 'repair', 'target': '#query', 'reason': 'candidate one', 'questions': []},
                {'status': 'repair', 'target': '#ready', 'reason': 'candidate two', 'questions': []}, analysis()])
            result = await self.service(model, origin).run(self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'inconclusive', result)
            self.assertEqual(len(result.report['repairs']), 2)
            self.assertEqual(len(model.calls), 5)

    async def test_report_budget_failure_keeps_observed_verdict(self):
        with site() as (origin, _, _):
            model = ScriptedModel([intake(), plan(origin + '/app/mixed')])
            result = await self.service(model, origin, limits=QALimits(max_model_calls=2)).run(
                self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'completed', result)
            self.assertEqual(result.error_code, 'MODEL_CALL_LIMIT')
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')

    async def test_unknown_expectation_never_starts_browser(self):
        with site() as (origin, hits, _):
            data = intake()
            data['expectation_source'] = 'unknown'
            model = ScriptedModel([data])
            result = await self.service(model, origin).run(self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'needs_info', result)
            self.assertTrue(result.questions)
            self.assertEqual(hits, [])


if __name__ == '__main__':
    unittest.main()
