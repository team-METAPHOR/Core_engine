import unittest
from core_engine.evaluation.human_timing import summarize_timings


class TimingTests(unittest.TestCase):
    def row(self, **changes):
        return {'pair_id': 'p1', 'case_id': 'c1', 'participant_id': 'person1', 'order': 'manual_first',
            'manual_active_seconds': '600', 'assisted_active_seconds': '300', 'manual_wall_seconds': '700',
            'assisted_wall_seconds': '900', 'manual_quality_pass': 'true', 'assisted_quality_pass': 'true',
            'hourly_cost_usd': '', 'assisted_api_cost_usd': '', **changes}

    def test_empty_baseline_and_missing_cost_never_become_zero_or_claim(self):
        self.assertEqual(summarize_timings([{}])['status'], 'not_measured')
        result = summarize_timings([self.row()])
        self.assertIsNone(result['net_cost_reduction_usd'])
        self.assertEqual(result['active_time_reduction_pct'], 50)
        self.assertEqual(result['assisted_median_wall_seconds'], 900)

    def test_failure_time_and_review_time_remain_in_denominator(self):
        result = summarize_timings([self.row(), self.row(pair_id='p2', order='assisted_first',
            assisted_active_seconds='800', assisted_quality_pass='false')])
        self.assertEqual(result['assisted_active_seconds'], 1100)
        self.assertEqual(result['assisted_quality_passes'], 1)
        self.assertEqual(result['both_quality_pass_pairs'], 1)

    def test_invalid_and_duplicate_measurements_are_rejected(self):
        for row in [self.row(manual_active_seconds='nan'), self.row(manual_active_seconds='800'),
                    self.row(assisted_quality_pass=''), self.row(order='')]:
            with self.assertRaises(ValueError):
                summarize_timings([row])
        with self.assertRaises(ValueError):
            summarize_timings([self.row(), self.row()])

    def test_cost_is_net_of_observed_api_charge_only_when_both_inputs_exist(self):
        result = summarize_timings([self.row(hourly_cost_usd='60', assisted_api_cost_usd='0.1')])
        self.assertAlmostEqual(result['net_cost_reduction_usd'], 4.9)
