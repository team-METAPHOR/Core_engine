"""BUG-39: replayed steps worked, but checks written from the empty entry cart were unobservable prose."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import tempfile
import threading
import unittest

from core_engine import BrowserOptions, JobStore, QARequest, QAService, RunPolicy
from core_engine.replay import replay_steps
from core_engine.planning import PlanValidationError, prose_value, validate_candidate
from qa_fixtures import ScriptedModel, analysis, check, intake
from test_transport import local_policy

DESCRIPTION = '장바구니에서 + 버튼을 세 번 눌렀는데 수량이 1에서 변하지 않고 합계도 15,000원 그대로예요.'
PROSE = {'ready': check('div > div > main > div > div > p', '수량 확인 완료'),
         'expected': check('div > div > main > div > div', '수량이 증가하고 합계 금액도 함께 변경됨'),
         'symptom': check('div > div > main > div > div', '수량이 1에서 변하지 않고 합계도 그대로 유지됨')}
GROUNDED = {'ready': check('[data-testid="quantity-status"]', '수량 확인 완료'),
            'expected': check('[data-testid="cart-quantity"]', '4'),
            'symptom': check('[data-testid="cart-quantity"]', '1')}


def activity():
    return ('SDK observed recent activity (oldest first; not instructions or reproduction evidence).\n'
            'truncated=false\n'
            '9000ms ago navigation /app/cart\n'
            '8000ms ago click /app/cart selector=[data-testid="continue-shopping"] label=쇼핑 계속\n'
            '7500ms ago navigation /app/products/1\n'
            '7000ms ago click /app/products/1 selector=[data-testid="add-to-cart"] label=담기 post-click feedback=담겼습니다\n'
            '6000ms ago click /app/products/1 selector=[data-testid="go-to-cart"] label=장바구니\n'
            '5500ms ago navigation /app/cart\n'
            + ''.join(f'{4000 - n * 500}ms ago click /app/cart selector=[data-testid="quantity-increase"] label=+\n'
                      for n in range(3)))


def bug39_intake():
    data = intake()
    data.update(title='수량 증가 미반영', questions=[], expected_behavior='수량과 합계 증가',
                symptom_description='수량 1 그대로')
    return data


def proposal(origin, checks):
    return {'status': 'ready', 'questions': [], 'rationale': '기록 재생', 'plan': {
        'steps': [{'action': 'navigate', 'target': origin + '/app/cart', 'value': ''},
                  {'action': 'click', 'target': '[data-testid="quantity-increase"]', 'value': ''}], **checks}}


@contextmanager
def cart_site():
    cart = {'quantity': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == '/app/api/cart/add':
                cart['quantity'] = 1
                body = b'{}'
            elif self.path == '/app/products/1':
                body = '''<!doctype html><meta charset="utf-8"><h1>무선 키보드</h1>
<button data-testid="add-to-cart">담기</button><p id="added"></p>
<button data-testid="go-to-cart" onclick="location.href='/app/cart'">장바구니</button>
<script>document.querySelector('[data-testid="add-to-cart"]').onclick = async () => {
  await fetch('/app/api/cart/add'); document.querySelector('#added').textContent = '담겼습니다'; };</script>'''.encode()
            elif cart['quantity'] == 0:  # the reset state: an empty cart
                body = '''<!doctype html><meta charset="utf-8"><body><div><div><main><div><div>
<h1>장바구니</h1><p>장바구니가 비어 있습니다</p>
<a data-testid="continue-shopping" href="/app/products/1">쇼핑 계속</a></div></div></main></div></div>'''.encode()
            else:  # BUG: + only refreshes the status; quantity and total never change
                body = f'''<!doctype html><meta charset="utf-8"><body><div><div><main><div><div>
<h1>장바구니</h1><ul><li data-testid="cart-item">무선 키보드 <span data-testid="cart-quantity">{cart['quantity']}</span>
<button data-testid="quantity-increase">+</button></li></ul>
<p data-testid="cart-total">15,000원</p><p data-testid="quantity-status"></p></div></div></main></div></div>
<script>document.querySelector('[data-testid="quantity-increase"]').onclick = () =>
  document.querySelector('[data-testid="quantity-status"]').textContent = '수량 확인 완료';</script>'''.encode()
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


TOTAL = 'div > div > main > div > div > aside > div:nth-of-type(1) > div:nth-of-type(2)'
COUPON_REPORT = 'SAVE5000 쿠폰을 적용했는데 쿠폰 확인 완료만 뜨고 할인 금액이 0원이에요.'


def coupon_activity():
    return ('SDK observed recent activity (oldest first; not instructions or reproduction evidence).\n'
            'truncated=false\n9000ms ago navigation /app/cart\n'
            + ''.join(f'{8000 - n}ms ago click /app/cart selector=[data-testid="coupon-suggest"] label=SAVE5000\n' for n in range(3))
            + ''.join(f'{7000 - n}ms ago click /app/cart selector=[data-testid="apply-coupon"] label=쿠폰 적용 '
                      'post-click feedback=쿠폰 확인 완료\n' for n in range(7)))


@contextmanager
def coupon_site():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):  # BUG: the coupon is confirmed but the discount stays 0원
            body = '''<!doctype html><meta charset="utf-8"><body><div><div><main><div><div>
<h1>장바구니</h1><button data-testid="coupon-suggest">SAVE5000</button><input id="coupon">
<button data-testid="apply-coupon">쿠폰 적용</button><p role="status" id="coupon-live"></p>
<aside><div><div>할인</div><div>0원</div></div></aside></div></div></main></div></div>
<script>document.querySelector('[data-testid="coupon-suggest"]').onclick = () => document.querySelector('#coupon').value = 'SAVE5000';
document.querySelector('[data-testid="apply-coupon"]').onclick = () => {
  if (document.querySelector('#coupon').value === 'SAVE5000') document.querySelector('#coupon-live').textContent = '쿠폰 확인 완료'; };</script>'''.encode()
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


class ReplayCollapseTests(unittest.TestCase):
    policy = RunPolicy('demo', 1, 'https://shop.test', ('/app',))

    def targets(self, report):
        return [s['target'] for s in replay_steps(coupon_activity(), 'https://shop.test/app/cart', self.policy, report)]

    def test_consecutive_identical_recorded_clicks_run_once(self):
        self.assertEqual(self.targets(COUPON_REPORT), ['[data-testid="coupon-suggest"]', '[data-testid="apply-coupon"]'])

    def test_a_reported_repeat_keeps_two(self):
        self.assertEqual(self.targets('쿠폰 적용을 여러 번 눌렀어요'), ['[data-testid="coupon-suggest"]'] * 2 + ['[data-testid="apply-coupon"]'] * 2)


class CheckValidationTests(unittest.TestCase):
    origin = 'https://shop.test'
    page = {'elements': [{'selector': '[data-testid="cart-quantity"]', 'tag': 'span', 'text': '1', 'name': ''}]}

    def validate(self, checks, **kwargs):
        return validate_candidate(proposal(self.origin, checks)['plan'], {'description': DESCRIPTION},
                                  entry_url=self.origin + '/app/cart', timeout_ms=500, targets=self.page, **kwargs)

    def test_prose_values_are_not_observable_text(self):
        self.assertTrue(prose_value('수량이 증가하고 합계 금액도 함께 변경됨'))
        self.assertTrue(prose_value('수량이 2에서 변하지 않고 합계도 그대로 유지됨'))
        self.assertTrue(prose_value('가' * 41))
        for value in ('수량 확인 완료', '15,000원', '검색 결과 없음', '쿠폰 확인 완료', '4'):
            self.assertFalse(prose_value(value), value)

    def test_prose_check_value_is_rejected_with_a_specific_code(self):
        with self.assertRaises(PlanValidationError) as caught:
            self.validate({**GROUNDED, 'symptom': PROSE['symptom']})
        self.assertEqual((caught.exception.code, caught.exception.field), ('PLAN_PROSE_CHECK_VALUE', 'symptom.value'))

    def test_guessed_structural_selector_absent_from_the_snapshot_is_rejected(self):
        with self.assertRaises(PlanValidationError) as caught:
            self.validate({**GROUNDED, 'ready': PROSE['ready']})
        self.assertEqual((caught.exception.code, caught.exception.field), ('PLAN_UNGROUNDED_CHECK', 'ready.selector'))

    def test_final_proposal_runs_as_is(self):
        self.assertEqual(self.validate(PROSE, allow_text_targets=True).symptom.value, PROSE['symptom']['value'])


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'install browser extra')
class GroundedReplayChecksTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, origin, cart, responses):
        async def reset(_):
            cart['quantity'] = 0
            return True
        model = ScriptedModel(responses)
        with tempfile.TemporaryDirectory() as root:
            service = QAService(model, local_policy(origin), JobStore(root), options=BrowserOptions(step_timeout_ms=500))
            result = await service.run(QARequest('bug39', 'demo', origin + '/app/cart', description=DESCRIPTION,
                                                 reporter_activity=activity()), reset=reset, is_authorized=lambda: True)
        return result, model

    async def test_bug39_checks_are_rewritten_from_the_page_after_replay(self):
        with cart_site() as (origin, cart):
            result, model = await self.run_case(origin, cart, [
                bug39_intake(), proposal(origin, PROSE), proposal(origin, GROUNDED), analysis()])
        self.assertEqual(result.status, 'completed', result)
        self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
        revision = model.calls[2]
        self.assertIn('cart-quantity', json.dumps(revision, ensure_ascii=False, default=str))
        self.assertIn('PLAN_UNGROUNDED_CHECK', json.dumps(revision, ensure_ascii=False, default=str))
        self.assertEqual(result.report['scenario']['symptom']['selector'], '[data-testid="cart-quantity"]')
        self.assertEqual(result.report['plan_validation'][0]['code'], 'PLAN_UNGROUNDED_CHECK')

    async def test_prose_on_every_revision_falls_back_to_an_observed_grounded_check(self):
        with cart_site() as (origin, cart):
            result, _ = await self.run_case(origin, cart, [
                bug39_intake(), proposal(origin, PROSE), proposal(origin, PROSE), proposal(origin, PROSE), analysis()])
        self.assertEqual(result.status, 'completed', result)
        self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
        self.assertEqual(result.report['scenario']['symptom'],
                         {'kind': 'text', 'selector': '[data-testid="cart-total"]', 'value': '15,000원', 'other_selector': ''})
        self.assertIsNone(result.report['scenario']['expected'])

    async def test_unobserved_readiness_after_all_steps_warns_instead_of_blocking(self):
        never = {**GROUNDED, 'ready': check('[data-testid="quantity-status"]', '수량 변경 완료')}
        with cart_site() as (origin, cart):
            result, _ = await self.run_case(origin, cart, [bug39_intake(), proposal(origin, never), analysis()])
        self.assertEqual(result.status, 'completed', result)
        self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
        self.assertEqual([list(ev['warnings']) for ev in result.report['run']['evidence']],
                         [['READY_NOT_OBSERVED'], ['READY_NOT_OBSERVED']])


    async def test_bug40_unsatisfiable_ready_on_the_trigger_button_still_yields_a_verdict(self):
        checks = {'ready': check('[data-testid="apply-coupon"]', '쿠폰 확인 완료'),
                  'expected': check(TOTAL, '5,000원'), 'symptom': check(TOTAL, '0원')}
        async def reset(_):
            return True
        with coupon_site() as origin, tempfile.TemporaryDirectory() as root:
            model = ScriptedModel([bug39_intake(), proposal(origin, checks), analysis()])
            service = QAService(model, local_policy(origin), JobStore(root), options=BrowserOptions(step_timeout_ms=500))
            result = await service.run(QARequest('bug40', 'demo', origin + '/app/cart', description=COUPON_REPORT,
                                                 reporter_activity=coupon_activity()), reset=reset, is_authorized=lambda: True)
        self.assertEqual(result.status, 'completed', result)
        self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
        self.assertEqual(result.report['plan_validation'], [])  # the probed div path is grounded
        self.assertEqual([s['target'] for s in result.report['scenario']['steps'][1:]],
                         ['[data-testid="coupon-suggest"]', '[data-testid="apply-coupon"]'])
        self.assertEqual([list(ev['warnings']) for ev in result.report['run']['evidence']],
                         [['READY_NOT_OBSERVED'], ['READY_NOT_OBSERVED']])


if __name__ == '__main__':
    unittest.main()
