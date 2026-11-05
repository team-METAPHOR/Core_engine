"""'Should change but stays the same' reports: verdicts compare the value before/after the final action."""
import unittest

from core_engine import BrowserPlan, BrowserRunner, Check, Step
from core_engine.qa_schema import PLAN_PROMPT
from fixture_site import site
from test_transport import local_policy


async def reset(_):
    return True


QTY = '[data-testid="cart-quantity"]'


def cart_plan(origin, path, expected, symptom):
    steps = (Step('navigate', origin + path), Step('click', '#add'), Step('click', '#inc'))
    return BrowserPlan(steps, Check('text', '#status', '처리됨'), symptom, expected=expected)


class RelativeCheckTests(unittest.IsolatedAsyncioTestCase):
    async def run_plan(self, plan, origin):
        return await BrowserRunner(local_policy(origin)).run(plan, reset=reset, is_authorized=lambda: True)

    async def test_relative_tokens_judge_quantity_change(self):
        with site() as (origin, _, _):
            for path, verdict in (('/app/cart-qty', 'reproduced'), ('/app/cart-qty-fixed', 'not_reproduced')):
                plan = cart_plan(origin, path, Check('text', QTY, '$changed'), Check('text', QTY, '$unchanged'))
                result = await self.run_plan(plan, origin)
                self.assertEqual(result.verdict.value, verdict, result)

    async def test_bug43_absolute_values_from_reporter_session_fall_back_to_before_after(self):
        # The reporter's session values differ from the reset state, so neither literal matches.
        with site() as (origin, _, _):
            for path, values, verdict in (('/app/cart-qty', ('3', '2'), 'reproduced'),
                                          ('/app/cart-qty-fixed', ('5', '4'), 'not_reproduced')):
                plan = cart_plan(origin, path, Check('text', QTY, values[0]), Check('text', QTY, values[1]))
                result = await self.run_plan(plan, origin)
                self.assertEqual(result.verdict.value, verdict, result)

    async def test_toggle_that_does_not_apply(self):
        with site() as (origin, _, _):
            for path, verdict in (('/app/toggle', 'reproduced'), ('/app/toggle-fixed', 'not_reproduced')):
                steps = (Step('navigate', origin + path), Step('click', '#toggle'))
                for expected, symptom in ((Check('text', '#mode', '$changed'), Check('text', '#mode', '$unchanged')),
                                          (Check('text', '#mode', 'Dark'), Check('text', '#mode', 'Light'))):
                    plan = BrowserPlan(steps, Check('text', '#status', '처리됨'), symptom, expected=expected)
                    result = await self.run_plan(plan, origin)
                    self.assertEqual(result.verdict.value, verdict, (path, expected, result))

    async def test_different_selectors_stay_inconclusive(self):
        with site() as (origin, _, _):
            plan = cart_plan(origin, '/app/cart-qty', Check('text', QTY, '3'), Check('text', '#status', '오류'))
            result = await self.run_plan(plan, origin)
            self.assertEqual(result.verdict.value, 'inconclusive', result)


    async def test_relative_check_without_baseline_is_inconclusive(self):
        # A navigate as final action leaves no before-value to compare against.
        with site() as (origin, _, _):
            plan = BrowserPlan((Step('navigate', origin + '/app/toggle'),), Check('visible', '#mode', 'true'),
                               Check('text', '#mode', '$unchanged'), expected=Check('text', '#mode', '$changed'))
            result = await self.run_plan(plan, origin)
            self.assertEqual(result.verdict.value, 'inconclusive', result)
            self.assertEqual(result.attempts[0].blocker, 'RELATIVE_BASELINE_UNAVAILABLE')

    def test_plan_prompt_teaches_relative_checks(self):
        self.assertIn('value="$unchanged"', PLAN_PROMPT)
        self.assertIn('value="$changed"', PLAN_PROMPT)
        self.assertIn('reset state', PLAN_PROMPT)


if __name__ == '__main__':
    unittest.main()
