"""A named start screen that the entry page cannot reach: go to the site root, then click the named item."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import tempfile
import threading
import unittest

from core_engine import BrowserOptions, JobStore, QARequest, QAService, RunPolicy
from core_engine.planning import PlanValidationError, validate_candidate
from core_engine.scenario_contract import ground_contract
from qa_fixtures import ScriptedModel, analysis, check, intake

DESCRIPTION = ('사전 상태: 로그인 완료, 빈 장바구니. 시작 화면: BugLab 저소음 마우스 상품 상세 페이지. '
               '1. [data-testid=add-to-cart] 클릭 2. [data-testid=go-to-cart] 클릭 '
               '3. 마우스 행 [data-testid=cart-item][data-product-id=2] 안의 [data-testid=quantity-increase] 클릭. '
               '수량이 2가 되어야 하는데 1 그대로입니다.')
REPORT = {'description': DESCRIPTION, 'expected_result': '수량 2', 'test_data': ''}
ADD, GO = '[data-testid="add-to-cart"]', '[data-testid="go-to-cart"]'
INCREASE = '[data-testid="cart-item"][data-product-id="2"] [data-testid="quantity-increase"]'
CARD = 'a[href="/products/2"]'


def report_intake():
    data = intake()
    data.update(title='수량 증가 무반응', questions=[], expected_behavior='수량 2', actions_complete=True,
                symptom_description='수량 1 그대로', preconditions=['빈 장바구니'], required_actions=[
                    {'action': 'click', 'value': '', 'source_quote': '[data-testid=add-to-cart] 클릭'},
                    {'action': 'click', 'value': '', 'source_quote': '[data-testid=go-to-cart] 클릭'},
                    {'action': 'click', 'value': '', 'source_quote': '[data-testid=quantity-increase] 클릭'}])
    return data


def plan(origin, *reach):
    return {'status': 'ready', 'questions': [], 'rationale': '상품 상세에서 담고 장바구니에서 수량 증가', 'plan': {
        'steps': [{'action': 'navigate', 'target': origin + '/cart', 'value': ''}, *reach,
                  *({'action': 'click', 'target': target, 'value': ''} for target in (ADD, GO, INCREASE))],
        'ready': check('#status', '변경됨'), 'expected': check('#quantity', '2'), 'symptom': check('#quantity', '1')}}


def root_reach(origin, root='/'):
    return [{'action': 'navigate', 'target': origin + root, 'value': ''},
            {'action': 'click', 'target': CARD, 'value': ''}]


def empty_cart(origin):
    return {'url': origin + '/cart', 'elements': [
        {'selector': 'h1', 'tag': 'h1', 'role': '', 'name': '', 'text': '장바구니가 비었습니다', 'enabled': True,
         'input_type': '', 'state_attributes': {}, 'href': ''}]}


class RootReachingValidationTests(unittest.TestCase):
    origin = 'https://shop.test'

    def validate(self, generated, page=None, report=REPORT):
        contract = {**ground_contract(report_intake(), report), 'actions_complete': True}
        return validate_candidate(generated['plan'], report, entry_url=self.origin + '/cart',
                                  timeout_ms=3000, contract=contract, page=page)

    def test_root_then_named_item_click_reaches_an_unlinked_start_screen(self):
        for page in (empty_cart(self.origin), None):
            with self.subTest(page=bool(page)):
                result = self.validate(plan(self.origin, *root_reach(self.origin)), page)
                self.assertEqual([s.target for s in result.steps][1:3], [self.origin + '/', CARD])

    def test_without_reaching_the_named_screen_is_still_asked_to_revise(self):
        with self.assertRaises(PlanValidationError) as error:
            self.validate(plan(self.origin), empty_cart(self.origin))
        self.assertEqual(error.exception.code, 'PLAN_SCREEN_NOT_REACHED')
        self.assertIn('"/"', error.exception.feedback)

    def test_other_destinations_or_unnamed_screens_stay_unsupported(self):
        with self.assertRaises(PlanValidationError) as error:
            self.validate(plan(self.origin, *root_reach(self.origin, '/admin')), empty_cart(self.origin))
        self.assertEqual(error.exception.code, 'PLAN_UNSUPPORTED_NAVIGATION')
        plain = {**REPORT, 'description': '[data-testid=add-to-cart] 클릭, [data-testid=go-to-cart] 클릭, '
                                          '[data-testid=quantity-increase] 클릭하면 수량이 그대로'}
        with self.assertRaises(PlanValidationError) as error:
            self.validate(plan(self.origin, *root_reach(self.origin)), None, plain)
        self.assertEqual(error.exception.code, 'PLAN_UNSUPPORTED_NAVIGATION')


@contextmanager
def shop():
    cart = {'items': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == '/api/cart/add':
                cart['items'] = 1
                body = b'{}'
            elif self.path == '/':
                body = '<!doctype html><meta charset="utf-8"><h1>BugLab</h1><a href="/products/2">BugLab 저소음 마우스</a>'.encode()
            elif self.path == '/products/2':
                body = '''<!doctype html><meta charset="utf-8"><h1>BugLab 저소음 마우스</h1>
<button data-testid="add-to-cart" onclick="fetch('/api/cart/add').then(() => document.querySelector('#added').textContent = '담김')">담기</button>
<p id="added"></p><a data-testid="go-to-cart" href="/cart">장바구니</a>'''.encode()
            else:
                row = ('<div data-testid="cart-item" data-product-id="2">마우스 <span id="quantity">1</span>'
                       '<button data-testid="quantity-increase" onclick="document.querySelector(\'#status\').textContent=\'변경됨\'">+</button></div>'
                       '<p id="status"></p>') if cart['items'] else '<h1>장바구니가 비었습니다</h1>'
                body = f'<!doctype html><meta charset="utf-8">{row}'.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', cart
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'install browser extra')
class RootReachingBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_cart_entry_reaches_the_product_through_the_site_root(self):
        with shop() as (origin, cart), tempfile.TemporaryDirectory() as root:
            async def reset(_):
                cart['items'] = 0
                return True
            policy = RunPolicy('demo', 1, origin, ('/',), allow_private_targets=True, private_cidrs=('127.0.0.1/32',))
            model = ScriptedModel([report_intake(), plan(origin), plan(origin, *root_reach(origin)), analysis()])
            service = QAService(model, policy, JobStore(root), options=BrowserOptions(step_timeout_ms=500))
            result = await service.run(QARequest('cart-qty', 'demo', origin + '/cart', description=DESCRIPTION,
                                                 expected_result='수량 2'), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'completed', result)
            self.assertEqual([v['code'] for v in result.report['plan_validation']], ['PLAN_SCREEN_NOT_REACHED'])
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
            self.assertEqual([s['target'] for s in result.report['scenario']['steps']][1:3], [origin + '/', CARD])


if __name__ == '__main__':
    unittest.main()
