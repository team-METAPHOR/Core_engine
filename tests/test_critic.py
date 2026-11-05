import copy
import importlib.util
import tempfile
import unittest

from core_engine import BrowserOptions, JobStore, ModelError, QALimits, QARequest, QAService
from core_engine.critic import REVIEW_SCHEMA, normalize_review, review_context
from core_engine.analysis_schema import validate_output
from fixture_site import site
from qa_fixtures import ScriptedModel, analysis, intake, plan, screenshot
from test_transport import local_policy


async def reset(_):
    return True


def review(decision='accept'):
    item = {'decision': decision, 'summary': '원문 제보의 순서와 검사 조건을 대조했습니다.'}
    if decision == 'revise':
        item['issues'] = [{'code': 'MISSING_STEP', 'field': 'steps', 'reason': '첫 번째 정상 검색을 복원하세요.',
                           'source_refs': ['report', 'proposal']}]
    if decision == 'needs_info':
        item['questions'] = ['첫 번째 검색어를 알려주세요.']
    return {'assessment': item}


class CriticContractTests(unittest.TestCase):
    def test_malformed_review_cannot_smuggle_replacement_plan_or_verdict(self):
        for changed in ({**review(), 'plan': {}}, {**review(), 'verdict': 'reproduced'},
                        {'assessment': {**review()['assessment'], 'issues': [{}]}},
                        {'assessment': {**review('revise')['assessment'], 'issues': []}},
                        {'assessment': {**review('needs_info')['assessment'], 'questions': []}}):
            with self.assertRaises(ValueError):
                validate_output(changed, REVIEW_SCHEMA)
        bad = review('revise')
        bad['assessment']['issues'][0]['source_refs'] = ['hidden-oracle']
        with self.assertRaises(ValueError):
            validate_output(bad, REVIEW_SCHEMA)
        for decision in ('accept', 'revise', 'needs_info'):
            value = review(decision)
            validate_output(value, REVIEW_SCHEMA)
            self.assertEqual(normalize_review(value)['decision'], decision)

    def test_only_report_and_visible_dom_are_shared(self):
        result = review_context({'description': 'synthetic'}, {'url': 'https://example.test', 'elements': [],
            'network': ['private'], 'errors': ['private'], 'image': {'name': 'private'}, 'secret': 'private'},
            {'status': 'ready', 'questions': [], 'plan': {}, 'rationale': 'private'}, None)
        self.assertEqual(set(result['page']), {'url', 'elements'})
        self.assertNotIn('private', str(result))


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'install browser extra')
class CriticFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = JobStore(temp.name)

    def request(self, origin):
        return QARequest('report-1', 'demo', origin + '/app/mixed', screenshot(),
                         'cat 검색 후 missing 검색 시 이전 목록이 남음', '빈 목록 표시',
                         'cat은 결과 있음, missing은 결과 없음')

    def service(self, model, origin, reviewer, **kwargs):
        return QAService(model, local_policy(origin), self.store, reviewer=reviewer,
                         options=BrowserOptions(step_timeout_ms=150), **kwargs)

    async def test_accept_preserves_browser_truth_and_retest_skips_critic(self):
        with site() as (origin, _, state):
            primary = ScriptedModel([intake(), plan(origin + '/app/mixed'), analysis(), analysis()])
            critic = ScriptedModel([review()])
            service = self.service(primary, origin, critic)
            before = await service.run(self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(before.report['run']['verdict']['value'], 'reproduced', before)
            self.assertEqual(before.report['plan_review']['status'], 'accepted')
            self.assertEqual(before.report['plan_review']['reviewed_plan_sha256'], before.report['plan_sha256'])
            self.assertNotIn('images', critic.calls[0])
            self.assertNotIn('intake', critic.calls[0]['context'])
            self.assertEqual([r['role'] for r in before.report['model_calls']], ['primary', 'primary', 'critic', 'primary'])
            state['fixed'] = True
            after = await service.retest(before.job_id, reset=reset, is_authorized=lambda: True)
            self.assertEqual(after.report['run']['verdict']['value'], 'not_reproduced', after)
            self.assertEqual(before.report['plan_sha256'], after.report['plan_sha256'])
            self.assertEqual(after.report['plan_review']['status'], 'skipped_retest')
            self.assertEqual(len(critic.calls), 1)

    async def test_critic_restores_omitted_setup_once_before_execution(self):
        with site() as (origin, _, _):
            correct = plan(origin + '/app/mixed')
            bad = copy.deepcopy(correct)
            bad['plan']['steps'] = [bad['plan']['steps'][0], *bad['plan']['steps'][-2:]]
            primary = ScriptedModel([intake(), bad, correct, analysis()])
            critic = ScriptedModel([review('revise')])
            result = await self.service(primary, origin, critic).run(self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced', result)
            self.assertEqual(len(critic.calls), 1)
            self.assertEqual([r['phase'] for r in result.report['model_calls']], ['intake', 'plan', 'plan_review', 'plan_revision', 'report'])
            self.assertEqual(result.report['plan_review']['status'], 'revised_unreviewed')
            self.assertNotEqual(result.report['plan_review']['reviewed_plan_sha256'], result.report['plan_sha256'])
            self.assertIn('review_feedback', primary.calls[2]['context'])

    async def test_unnecessary_question_can_be_revised_but_real_question_stops(self):
        with site() as (origin, _, _):
            unanswered = {'status': 'needs_info', 'questions': ['어떤 검색어인가요?'], 'rationale': '확인 필요', 'plan': None}
            primary = ScriptedModel([intake(), unanswered, plan(origin + '/app/mixed'), analysis()])
            result = await self.service(primary, origin, ScriptedModel([review('revise')])).run(
                self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'completed', result)
            self.assertEqual(result.report['plan_review']['status'], 'revised_unreviewed')
            primary = ScriptedModel([intake(), plan(origin + '/app/mixed')])
            result = await self.service(primary, origin, ScriptedModel([review('needs_info')])).run(
                self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'needs_info')
            self.assertTrue(result.questions)
            self.assertIsNone(result.report['run'])

    async def test_accept_cannot_override_host_rejection(self):
        with site() as (origin, _, _):
            bad = plan(origin + '/app/mixed')
            bad['plan']['ready'] = bad['plan']['expected']
            primary, critic = ScriptedModel([intake(), bad]), ScriptedModel([review()])
            result = await self.service(primary, origin, critic, limits=QALimits(max_plan_revisions=0)).run(
                self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.error_code, 'INVALID_PLAN_OR_BASELINE', result)
            self.assertIsNone(result.report['run'])
            self.assertEqual(critic.calls[0]['context']['validation']['code'], 'PLAN_READINESS_IS_OUTCOME')
            self.assertEqual(result.report['plan_review']['status'], 'revision_exhausted')

    async def test_critic_failure_is_explicit_and_stops_reproduction(self):
        with site() as (origin, _, _):
            primary = ScriptedModel([intake(), plan(origin + '/app/mixed')])
            critic = ScriptedModel([ModelError('MODEL_RATE_LIMITED')])
            result = await self.service(primary, origin, critic).run(self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.error_code, 'MODEL_RATE_LIMITED', result)
            self.assertEqual(result.report['plan_review']['status'], 'failed')
            self.assertIsNone(result.report['run'])
            self.assertEqual(len(critic.calls), 1)
            self.assertEqual(len(primary.calls), 2)

    async def test_shared_budget_counts_critic_and_preserves_executed_verdict(self):
        with site() as (origin, _, _):
            primary = ScriptedModel([intake(), plan(origin + '/app/mixed')])
            critic = ScriptedModel([review()])
            result = await self.service(primary, origin, critic, limits=QALimits(max_model_calls=3)).run(
                self.request(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.error_code, 'MODEL_CALL_LIMIT', result)
            self.assertEqual(result.status, 'completed')
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
            self.assertEqual(len(result.report['model_calls']), 3)

    async def test_revocation_during_critic_prevents_browser_execution(self):
        with site() as (origin, _, _):
            authorized = [True]
            def revoke(_):
                authorized[0] = False
                return review()
            primary = ScriptedModel([intake(), plan(origin + '/app/mixed')])
            result = await self.service(primary, origin, ScriptedModel([revoke])).run(
                self.request(origin), reset=reset, is_authorized=lambda: authorized[0])
            self.assertEqual(result.error_code, 'AUTHORIZATION_REVOKED', result)
            self.assertIsNone(result.report['run'])
