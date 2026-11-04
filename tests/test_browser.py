from dataclasses import asdict
import asyncio
import importlib.util
import json
import threading
import unittest

from core_engine import BudgetLimits
from core_engine.browser import BrowserRunner
from core_engine.plan import BrowserPlan, Step, TextCheck
from fixture_site import site
from test_transport import local_policy


def search_plan(origin, path="/app/bug"):
    return BrowserPlan((
        Step("navigate", origin + path),
        Step("assert_text", "#fresh", "yes"),
        Step("fill", "#query", "cat"), Step("click", "#search"),
        Step("assert_text", "#result", "cat"),
        Step("fill", "#query", "private-search-value"), Step("click", "#search"),
    ), ready=TextCheck("#ready", "done"), symptom=TextCheck("#result", "cat"))


class PlanTests(unittest.TestCase):
    def test_executable_or_unbounded_plans_rejected(self):
        with self.assertRaises(ValueError):
            Step("evaluate", "alert(1)")
        with self.assertRaises(ValueError):
            Step("click", "#button", "unexpected")
        with self.assertRaises(ValueError):
            BrowserPlan(tuple(Step("navigate", "https://example.test") for _ in range(21)), TextCheck("#ready", "yes"), TextCheck("#result", "bad"))


@unittest.skipUnless(importlib.util.find_spec("playwright"), "install .[browser] and Chromium for browser tests")
class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_bug_and_fixed_page_with_independent_contexts(self):
        with site() as (origin, _, _):
            resets = []
            async def reset(attempt):
                resets.append(attempt)
                return True
            runner = BrowserRunner(local_policy(origin))
            for path, expected in (("/app/bug", "reproduced"), ("/app/fixed", "not_reproduced")):
                result = await runner.run(search_plan(origin, path), reset=reset, is_authorized=lambda: True)
                self.assertEqual(result.verdict.value, expected, result)
                self.assertEqual(len(set(result.verdict.evidence_ids)), 2)
                self.assertNotIn("private-search-value", json.dumps(asdict(result)))
            self.assertEqual(resets, [1, 2, 1, 2])

    async def test_different_attempts_are_inconclusive(self):
        with site() as (origin, _, state):
            async def reset(attempt):
                state["fixed"] = attempt == 2
                return True
            result = await BrowserRunner(local_policy(origin)).run(search_plan(origin, "/app/mixed"), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.reason_code, "ATTEMPTS_DISAGREE")

    async def test_blocked_browser_requests_are_never_sent(self):
        with site() as (origin, hits, _):
            async def reset(_):
                return True
            for path in ("/app/post", "/app/outside", "/app/websocket", "/app/redirect"):
                plan = BrowserPlan((Step("navigate", origin + path),), TextCheck("#ready", "ready"), TextCheck("#result", "cat"))
                result = await BrowserRunner(local_policy(origin)).run(plan, reset=reset, is_authorized=lambda: True)
                self.assertEqual(result.verdict.value, "inconclusive", (path, result))
            self.assertFalse(any(method == "POST" or path in {"/outside", "/app/ws"} for method, path, _ in hits), hits)

    async def test_reset_and_readiness_failures_do_not_become_negative_verdicts(self):
        with site() as (origin, _, _):
            async def failed_reset(_):
                return False
            runner = BrowserRunner(local_policy(origin))
            result = await runner.run(search_plan(origin), reset=failed_reset, is_authorized=lambda: True)
            self.assertEqual(result.attempts[0].blocker, "RESET_FAILED")
            async def reset(_):
                return True
            plan = BrowserPlan((Step("navigate", origin + "/app/bug"),), TextCheck("#missing", "ready"), TextCheck("#result", "cat"), step_timeout_ms=100)
            result = await runner.run(plan, reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.value, "inconclusive")
            self.assertEqual(result.attempts[0].blocker, "STEP_OR_READINESS_FAILED")

    async def test_cancelled_and_exhausted_run(self):
        with site() as (origin, hits, _):
            async def reset(_):
                return True
            cancelled = threading.Event()
            cancelled.set()
            result = await BrowserRunner(local_policy(origin)).run(search_plan(origin), reset=reset, is_authorized=lambda: True, cancelled=cancelled)
            self.assertEqual(result.attempts[0].blocker, "CANCELLED")
            self.assertEqual(hits, [])
            result = await BrowserRunner(local_policy(origin), BudgetLimits(max_total_requests=1)).run(search_plan(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.value, "inconclusive")
            self.assertEqual(result.attempts[1].blocker, "REQUEST_LIMIT")

    async def test_runtime_deadline_includes_environment_reset(self):
        with site() as (origin, hits, _):
            async def reset(_):
                await asyncio.sleep(5)
                return True
            result = await BrowserRunner(local_policy(origin), BudgetLimits(max_runtime_seconds=1)).run(
                search_plan(origin), reset=reset, is_authorized=lambda: True,
            )
            self.assertEqual(result.verdict.value, "inconclusive")
            self.assertEqual(result.attempts[0].blocker, "RUNTIME_LIMIT")
            self.assertEqual(hits, [])
