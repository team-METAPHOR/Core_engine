import unittest
from core_engine.diagnosis import build_diagnosis


class DiagnosisTests(unittest.TestCase):
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
