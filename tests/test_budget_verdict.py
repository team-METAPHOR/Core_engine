import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core_engine import AttemptResult, BudgetLimits, PolicyError, RequestBudget, judge_attempts


class BudgetTests(unittest.TestCase):
    def test_concurrency_count_and_time_limits(self):
        now = [0.0]
        budget = RequestBudget(
            BudgetLimits(max_concurrent_requests=1, max_total_requests=2, max_runtime_seconds=5),
            clock=lambda: now[0],
        )
        with budget.request():
            with self.assertRaises(PolicyError) as error:
                with budget.request():
                    pass
            self.assertEqual(error.exception.code, "CONCURRENCY_LIMIT")
        with budget.request():
            pass
        with self.assertRaises(PolicyError) as error:
            with budget.request():
                pass
        self.assertEqual(error.exception.code, "REQUEST_LIMIT")
        now[0] = 5.0
        with self.assertRaises(PolicyError) as error:
            budget.check_runtime()
        self.assertEqual(error.exception.code, "RUNTIME_LIMIT")

    def test_download_and_response_limits(self):
        budget = RequestBudget(BudgetLimits(max_download_bytes=10, max_response_bytes=5))
        budget.check_response_size(5)
        budget.received(8)
        with self.assertRaises(PolicyError) as response:
            budget.check_response_size(6)
        self.assertEqual(response.exception.code, "RESPONSE_LIMIT")
        with self.assertRaises(PolicyError) as download:
            budget.received(3)
        self.assertEqual(download.exception.code, "DOWNLOAD_LIMIT")

    def test_bool_is_not_a_numeric_budget(self):
        with self.assertRaises(PolicyError):
            BudgetLimits(max_total_requests=True)

    def test_rate_window_recovers_after_sixty_seconds(self):
        now = [0.0]
        budget = RequestBudget(BudgetLimits(max_requests_per_minute=1, max_runtime_seconds=120), clock=lambda: now[0])
        with budget.request():
            pass
        with self.assertRaises(PolicyError) as error:
            with budget.request():
                pass
        self.assertEqual(error.exception.code, "RATE_LIMIT")
        now[0] = 60.0
        with budget.request():
            pass


class VerdictTests(unittest.TestCase):
    def test_two_clean_observations_are_required(self):
        seen = AttemptResult(True, True, ("a",))
        absent = AttemptResult(True, False, ("b",))
        blocked = AttemptResult(False, None, blocker="scope_blocked")
        self.assertEqual(judge_attempts((seen, seen)).value, "reproduced")
        self.assertEqual(judge_attempts((absent, absent)).value, "not_reproduced")
        self.assertEqual(judge_attempts((seen, absent)).reason_code, "ATTEMPTS_DISAGREE")
        self.assertEqual(judge_attempts((seen, blocked)).value, "inconclusive")

    def test_unsubstantiated_attempt_cannot_be_conclusive(self):
        with self.assertRaises(ValueError):
            AttemptResult(True, True)
        with self.assertRaises(ValueError):
            AttemptResult(False, False, blocker="network_error")
        with self.assertRaises(ValueError):
            judge_attempts((AttemptResult(True, True, ("a",)),))


if __name__ == "__main__":
    unittest.main()
