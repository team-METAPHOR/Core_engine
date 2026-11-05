"""A reported literal (coupon/search code) typed right before a reported click is a grounded precondition (B01)."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import tempfile
import threading
import unittest

from core_engine import BrowserOptions, JobStore, QARequest, QAService
from core_engine.planning import PlanValidationError, validate_candidate
from core_engine.qa_schema import INTAKE_PROMPT
from core_engine.scenario_contract import confirm_contract, ground_contract
from qa_fixtures import ScriptedModel, analysis, check, intake
from test_transport import local_policy

DESCRIPTION = ('무선 키보드 담고 SAVE5000 쿠폰 적용을 눌렀는데 쿠폰 확인 완료만 뜨고 결제 금액이 30,000원 그대로예요. '
               '5,000원 할인이 안 됩니다.')
ACTIVITY = ('SDK observed recent activity (oldest first). truncated=false\n'
            '4000ms ago navigation /app/products/1\n'
            '3000ms ago click /app/products/1 selector=#add label=장바구니 담기\n'
            '2000ms ago click /app/products/1 selector=#go-cart label=장바구니\n'
            '1500ms ago navigation /app/cart\n'
            '500ms ago click /app/cart selector=#apply label=쿠폰 적용')
REPORT = {'description': DESCRIPTION, 'expected_result': '', 'test_data': '', 'reporter_activity': ACTIVITY}


def b01_intake():
    """The production intake: the coupon code became a click quote instead of a fill."""
    data = intake()
    data.update(title='쿠폰 적용 후 결제 금액 미반영', expected_behavior='5,000원 할인 반영', questions=[],
                symptom_description='결제 금액 30,000원 그대로', required_actions=[
                    {'action': 'click', 'value': '', 'source_quote': '무선 키보드 담고 SAVE5000 쿠폰 적용을 눌렀는데'},
                    {'action': 'click', 'value': '', 'source_quote': 'SAVE5000 쿠폰 적용'},
                    {'action': 'observe', 'value': '', 'source_quote': '쿠폰 확인 완료'},
                    {'action': 'observe', 'value': '', 'source_quote': '결제 금액이 30,000원 그대로'}])
    return data


FILL = {'action': 'fill', 'target': '#coupon', 'value': 'SAVE5000'}
APPLY = {'action': 'click', 'target': '#apply', 'value': ''}


def coupon_plan(origin, *steps, entry='/app/cart'):
    return {'status': 'ready', 'questions': [], 'rationale': '쿠폰 코드 입력 후 적용', 'plan': {
        'steps': [{'action': 'navigate', 'target': origin + entry, 'value': ''}, *steps],
        'ready': check('#coupon-status', '쿠폰 확인 완료'), 'expected': check('#total', '25,000원'),
        'symptom': check('#total', '30,000원')}}


class GroundedFillValidationTests(unittest.TestCase):
    origin = 'https://shop.test'

    def validate(self, steps, contract, report=REPORT):
        plan = coupon_plan(self.origin, *steps)['plan']
        return validate_candidate(plan, report, entry_url=self.origin + '/app/cart', timeout_ms=3000, contract=contract)

    def test_b01_plan_typing_the_reported_code_before_apply_is_valid(self):
        plan = self.validate([FILL, APPLY], ground_contract(b01_intake(), REPORT))
        self.assertEqual([(s.action, s.value) for s in plan.steps[1:]], [('fill', 'SAVE5000'), ('click', None)])

    def test_grounded_fill_is_not_an_added_interaction_in_a_complete_sequence(self):
        data = intake()
        data.update(required_actions=[{'action': 'click', 'value': '', 'source_quote': 'SAVE5000 쿠폰 적용'}],
                    actions_complete=True)
        contract = {**ground_contract(data, REPORT), 'actions_complete': True}
        self.validate([FILL, APPLY], contract)
        lowercase = {**FILL, 'value': 'save5000'}
        with self.assertRaises(PlanValidationError) as error:  # matching is case-insensitive, input stays verbatim
            self.validate([lowercase, APPLY], contract)
        self.assertEqual(error.exception.code, 'PLAN_UNSUPPORTED_INPUT')
        self.assertTrue(confirm_contract(self.validate([FILL, APPLY], ground_contract(data, REPORT)),
                                         ground_contract(data, REPORT), REPORT)['actions_complete'])

    def test_unreported_value_or_fill_not_before_a_click_stays_rejected(self):
        data = intake()
        data.update(required_actions=[{'action': 'click', 'value': '', 'source_quote': 'SAVE5000 쿠폰 적용'}])
        contract = {**ground_contract(data, REPORT), 'actions_complete': True}
        with self.assertRaises(PlanValidationError) as error:
            self.validate([{**FILL, 'value': 'WELCOME10'}, APPLY], contract)
        self.assertEqual(error.exception.code, 'PLAN_REPORTED_SEQUENCE_CHANGED')
        with self.assertRaises(PlanValidationError) as error:
            self.validate([APPLY, FILL], contract)
        self.assertEqual(error.exception.code, 'PLAN_REPORTED_SEQUENCE_CHANGED')
        with self.assertRaises(PlanValidationError) as error:  # still one reported click, not two
            self.validate([FILL, APPLY, APPLY], contract)
        self.assertEqual(error.exception.code, 'PLAN_UNREPORTED_REPEAT')

    def test_intake_prompt_turns_a_code_next_to_apply_into_a_fill(self):
        self.assertIn('SAVE5000', INTAKE_PROMPT)
        self.assertIn('fill', INTAKE_PROMPT.split('SAVE5000')[1][:200])



BUG31_ACTIVITY = ('SDK observed recent activity (oldest first). truncated=false\n'
                  '6000ms ago navigation /app/search\n'
                  '5000ms ago click /app/search selector=[data-testid="search-results"] > li:nth-of-type(1) > a label=무선 키보드\n'
                  '4500ms ago navigation /app/products/1\n'
                  '4000ms ago click /app/products/1 selector=[data-testid="add-to-cart"] label=장바구니 담기\n'
                  '3500ms ago click /app/products/1 selector=[data-testid="go-to-cart"] label=장바구니\n'
                  '3000ms ago navigation /app/cart\n'
                  '2000ms ago click /app/cart selector=[data-testid="quantity-decrease"] label=-\n'
                  '1000ms ago click /app/cart selector=#root > div > div:nth-of-type(3) > ul > li:nth-of-type(2) > button label=SAVE5000\n'
                  '500ms ago click /app/cart selector=[data-testid="apply-coupon"] label=쿠폰 적용')
BUG31_REPORT = {'description': '수량을 줄이고 SAVE5000 쿠폰을 적용하면 결제 금액이 이상해요.', 'expected_result': '',
                'test_data': '', 'reporter_activity': BUG31_ACTIVITY}
BUG31_CLICKS = ['[data-testid="search-results"] > li:nth-of-type(1) > a', '[data-testid="add-to-cart"]',
                '[data-testid="go-to-cart"]', '[data-testid="quantity-decrease"]',
                '#root > div > div:nth-of-type(3) > ul > li:nth-of-type(2) > button', '[data-testid="apply-coupon"]']


def bug31_intake(complete=False):
    """The production intake: recorded events became strict actions, starting with navigation to the report page."""
    data = intake()
    data.update(questions=[], actions_complete=complete, required_actions=[
        {'action': 'navigate', 'value': '/app/cart', 'source_quote': 'navigation /app/cart'},
        *({'action': 'click', 'value': '', 'source_quote': 'selector=' + selector} for selector in BUG31_CLICKS)])
    return data


def bug31_steps(origin, *, first=('navigate', '/app/search'), targets=None):
    targets = targets or ['#results li:first-child a', '#add', '#go-cart', '#decrease', '#suggest-SAVE5000', '#apply']
    return [{'action': first[0], 'target': origin + first[1], 'value': ''},
            *({'action': 'click', 'target': target, 'value': ''} for target in targets)]


class RecordedFlowStartTests(unittest.TestCase):
    """BUG-31: filed on /cart, but the recorded flow started on another in-scope page."""
    origin = 'https://shop.test'

    def validate(self, steps, contract, report=BUG31_REPORT):
        plan = coupon_plan(self.origin, *steps)['plan']
        return validate_candidate(plan, report, entry_url=self.origin + '/app/cart', timeout_ms=3000, contract=contract)

    def test_plan_starting_from_the_earliest_recorded_path_with_equivalent_targets_is_valid(self):
        for complete in (False, True):
            with self.subTest(actions_complete=complete):
                contract = {**ground_contract(bug31_intake(complete), BUG31_REPORT), 'actions_complete': complete}
                plan = self.validate(bug31_steps(self.origin), contract)
                self.assertEqual(plan.steps[1].target, self.origin + '/app/search')
                # Explicitly re-opening the report page after the cart click still satisfies the recorded navigation.
                steps = bug31_steps(self.origin)
                steps.insert(4, {'action': 'navigate', 'target': self.origin + '/app/cart', 'value': ''})
                self.validate(steps, contract)
        contract = ground_contract(bug31_intake(True), BUG31_REPORT)
        self.assertTrue(confirm_contract(self.validate(bug31_steps(self.origin), contract), contract,
                                         BUG31_REPORT)['actions_complete'])

    def test_unrecorded_navigation_and_dropped_recorded_clicks_stay_rejected(self):
        contract = ground_contract(bug31_intake(), BUG31_REPORT)
        with self.assertRaises(PlanValidationError) as error:
            self.validate(bug31_steps(self.origin, first=('navigate', '/app/admin')), contract)
        self.assertEqual(error.exception.code, 'PLAN_REPORTED_SEQUENCE_CHANGED')
        with self.assertRaises(PlanValidationError) as error:
            self.validate(bug31_steps(self.origin, targets=['#decrease', '#apply']), contract)
        self.assertEqual(error.exception.code, 'PLAN_REPORTED_SEQUENCE_CHANGED')
        other_origin = [{**bug31_steps(self.origin)[0], 'target': 'https://evil.test/app/search'},
                        *bug31_steps(self.origin)[1:]]
        with self.assertRaises(PlanValidationError):
            self.validate(other_origin, contract)
        without_activity = {**BUG31_REPORT, 'reporter_activity': ''}
        with self.assertRaises(PlanValidationError):
            self.validate(bug31_steps(self.origin), contract, without_activity)

    def test_prompts_start_from_the_recorded_flow_and_treat_selectors_as_order_evidence(self):
        from core_engine.qa_schema import PLAN_PROMPT
        self.assertIn('earliest recorded in-scope path', PLAN_PROMPT)
        self.assertIn('evidence of order', PLAN_PROMPT)
        self.assertIn('report page', INTAKE_PROMPT)


@contextmanager
def coupon_site():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            body = '''<!doctype html><meta charset="utf-8"><h1>장바구니</h1>
<p>무선 키보드 1개</p><input id="coupon" placeholder="쿠폰 코드"><button id="apply">쿠폰 적용</button>
<p id="coupon-status"></p><p>결제 금액 <span id="total">30,000원</span></p>
<script>document.querySelector('#apply').onclick = () => {
  document.querySelector('#coupon-status').textContent =
    document.querySelector('#coupon').value === 'SAVE5000' ? '쿠폰 확인 완료' : '쿠폰 코드를 입력하세요';
};</script>'''.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'install browser extra')
class GroundedFillBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_b01_report_reproduces_instead_of_failing_plan_validation(self):
        with coupon_site() as origin, tempfile.TemporaryDirectory() as root:
            model = ScriptedModel([b01_intake(), coupon_plan(origin, FILL, APPLY), analysis()])
            service = QAService(model, local_policy(origin), JobStore(root), options=BrowserOptions(step_timeout_ms=300))

            async def reset(_):
                return True
            result = await service.run(QARequest('b01', 'demo', origin + '/app/cart', description=DESCRIPTION,
                                                 reporter_activity=ACTIVITY), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'completed', result)
            self.assertEqual(result.report['plan_validation'], [])
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
            self.assertEqual([s['value'] for s in result.report['scenario']['steps'] if s['action'] == 'fill'],
                             ['SAVE5000'])


if __name__ == '__main__':
    unittest.main()
