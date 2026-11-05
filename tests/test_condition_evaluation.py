import unittest
from core_engine.evaluation.conditions import grade_matrix


class ConditionGradingTests(unittest.TestCase):
    def test_one_positive_in_normal_matrix_counts_as_parent_false_positive(self):
        ids = ('admin', 'staff', 'staff_firefox', 'staff_webkit_slow')
        report = {'status': 'completed', 'conditions': [{'condition_id': key,
            'verdict': 'reproduced' if key == 'staff' else 'not_reproduced'} for key in ids]}
        result = grade_matrix(report, {key: {'passed': True} for key in ids}, buggy=False)
        self.assertFalse(result['passed'])
        self.assertTrue(result['normal_matrix_false_positive'])
        self.assertEqual(result['false_positive_rows'], 1)

    def test_plausible_verdict_without_independent_actions_does_not_pass(self):
        ids = ('admin', 'staff', 'staff_firefox', 'staff_webkit_slow')
        report = {'status': 'completed', 'conditions': [{'condition_id': key, 'verdict': 'not_reproduced'} for key in ids]}
        result = grade_matrix(report, {key: {'passed': key != 'staff'} for key in ids}, buggy=False)
        self.assertFalse(result['passed'])
        self.assertEqual(sum(result['rows'].values()), 3)
