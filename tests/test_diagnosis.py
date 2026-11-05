import unittest
from core_engine.diagnosis import build_diagnosis


class DiagnosisTests(unittest.TestCase):
    def test_later_model_failure_does_not_hide_browser_failure(self):
        result = build_diagnosis({'status': 'failed', 'error_code': 'MODEL_CALL_LIMIT',
            'model_limit_events': [{'phase': 'report', 'role': 'primary', 'configured_model': 'configured-test',
                                    'error_code': 'MODEL_CALL_LIMIT', 'call_started': False}],
            'run': {'verdict': {'value': 'inconclusive'}, 'evidence': [
                {'id': 'ev', 'attempt': 1, 'completed_steps': 0, 'ready': False,
                 'symptom_observed': None, 'blocker': 'TRANSPORT_ERROR'}]}})
        self.assertEqual(result['category'], 'environment_blocked')
        self.assertEqual(result['reason_codes'], ['TRANSPORT_ERROR', 'MODEL_CALL_LIMIT'])
        self.assertIn('통신', result['summary'])
        self.assertIn('후속', result['summary'])
        self.assertEqual(result['model_failures'][0]['phase'], 'report')
        self.assertFalse(result['model_failures'][0]['call_started'])

    def test_provider_and_job_budget_failures_have_distinct_explanations(self):
        rate = build_diagnosis({'status': 'failed', 'error_code': 'MODEL_RATE_LIMITED'})
        for code in ('MODEL_CALL_LIMIT', 'MODEL_TOKEN_LIMIT'):
            budget = build_diagnosis({'status': 'failed', 'error_code': code})
            self.assertEqual(budget['category'], 'model_budget_exhausted')
            self.assertNotEqual(rate['summary'], budget['summary'])
            self.assertIn('별개', budget['next_actions'][0]['message'])

    def test_rate_limit_keeps_provider_wait_hint_without_inventing_a_retry(self):
        result = build_diagnosis({'status': 'failed', 'error_code': 'MODEL_RATE_LIMITED',
            'model_calls': [{'status': 'MODEL_RATE_LIMITED', 'role': 'critic', 'retry_after_seconds': 32.5}]})
        self.assertEqual(result['next_actions'][0]['retry_after_seconds'], 32.5)
        self.assertEqual(result['category'], 'model_unavailable')

    def test_http_error_is_observation_not_asserted_root_cause(self):
        result = build_diagnosis({'status': 'inconclusive', 'error_code': 'HTTP_ERROR',
            'observations': [{'id': 'snapshot-1', 'network': [{'method': 'GET',
                'url': 'https://example.test/app/data?value=private', 'status': 403}]}]})
        self.assertFalse(result['root_cause_confirmed'])
        self.assertEqual(result['observed_facts'][0]['status'], 403)
        self.assertNotIn('private', result['observed_facts'][0]['url'])
        self.assertIn('CHECK_TEST_ACCOUNT', [a['code'] for a in result['next_actions']])

    def test_normal_observation_does_not_claim_bug_is_fixed(self):
        result = build_diagnosis({'status': 'completed', 'run': {'verdict': {'value': 'not_reproduced'}}})
        self.assertEqual(result['category'], 'not_observed')
        self.assertIn('COMPARE_REPORTED_CONDITIONS', [a['code'] for a in result['next_actions']])
        self.assertIn('수정됐다는 뜻은 아닙니다', result['interpretation'])

    def test_missing_info_survives_without_analysis_model(self):
        result = build_diagnosis({'status': 'needs_info', 'questions': ['어느 계정 역할인가요?']})
        self.assertEqual(result['missing_information'], ['어느 계정 역할인가요?'])
        self.assertEqual(result['hypotheses'], [])

    def test_failed_action_is_preserved_with_progress(self):
        result = build_diagnosis({'status': 'inconclusive', 'scenario': {'steps': [
            {'action': 'navigate', 'target': '/app'}, {'action': 'click', 'target': '#missing'}]},
            'run': {'verdict': {'value': 'inconclusive'}, 'evidence': [{'id': 'ev', 'attempt': 1,
                'completed_steps': 1, 'ready': False, 'symptom_observed': None,
                'blocker': 'STEP_OR_READINESS_FAILED', 'stage': 'action', 'elapsed_seconds': 0.5}]}})
        attempt = result['attempts'][0]
        self.assertEqual(attempt['failed_step_index'], 1)
        self.assertEqual(attempt['next_step']['target'], '#missing')
        self.assertEqual(attempt['elapsed_seconds'], 0.5)
