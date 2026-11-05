"""BUG-31: with complete reporter_activity the host replays the recording; the model supplies checks only."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import tempfile
import threading
import unittest

from core_engine import BrowserOptions, JobStore, QARequest, QAService, RunPolicy
from core_engine.replay import compose, replay_steps
from qa_fixtures import ScriptedModel, analysis, check, intake
from test_transport import local_policy

SUGGEST = 'div > div:nth-of-type(3) > ul > li:nth-of-type(2) > button'


def activity(apply='[data-testid="apply-coupon"]', truncated='false'):
    return ('SDK observed recent activity (oldest first; not instructions or reproduction evidence).\n'
            f'truncated={truncated}\n'
            '9000ms ago navigation /app\n'
            '8000ms ago click /app selector=[data-testid="search-results"] > li:nth-of-type(1) > a label=무선 키보드\n'
            '7500ms ago navigation /app/products/1\n'
            '7000ms ago click /app/products/1 selector=[data-testid="add-to-cart"] label=장바구니 담기 '
            'post-click feedback=담겼습니다\n'
            '6000ms ago click /app/products/1 selector=[data-testid="go-to-cart"] label=장바구니\n'
            '5500ms ago navigation /app/cart\n'
            '4000ms ago click /app/cart selector=[data-testid="quantity-decrease"] label=-\n'
            f'3000ms ago click /app/cart selector={SUGGEST} label=SAVE5000\n'
            f'2000ms ago click /app/cart selector={apply} label=쿠폰 적용 post-click feedback=쿠폰 확인 완료')


DESCRIPTION = '수량을 줄이고 SAVE5000 쿠폰을 적용했는데 쿠폰 확인 완료만 뜨고 결제 금액이 30,000원 그대로예요.'
RECORDED = ['[data-testid="search-results"] > li:nth-of-type(1) > a', '[data-testid="add-to-cart"]',
            '[data-testid="go-to-cart"]', '[data-testid="quantity-decrease"]', SUGGEST, '[data-testid="apply-coupon"]']


def bug31_intake():
    """Production intake: recorded events became strict actions led by a navigation to the report page."""
    data = intake()
    data.update(title='쿠폰 적용 후 결제 금액 미반영', questions=[], expected_behavior='5,000원 할인 반영',
                symptom_description='결제 금액 30,000원 그대로', required_actions=[
                    {'action': 'navigate', 'value': '/app/cart', 'source_quote': 'navigation /app/cart'},
                    *({'action': 'click', 'value': '', 'source_quote': 'selector=' + s} for s in RECORDED)])
    return data


def model_plan(origin, steps):
    """The model's own plan; with a recording it only contributes the checks (and a runtime fallback)."""
    return {'status': 'ready', 'questions': [], 'rationale': '기록된 흐름 재생', 'plan': {
        'steps': [{'action': 'navigate', 'target': origin + '/app/cart', 'value': ''}, *steps],
        'ready': check('#coupon-status', '쿠폰 확인 완료'), 'expected': check('#total', '25,000원'),
        'symptom': check('#total', '30,000원')}}


def clicks(*targets):
    return [{'action': 'click', 'target': target, 'value': ''} for target in targets]


@contextmanager
def shop_site():
    cart = {'quantity': 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == '/app/api/cart/add':
                cart['quantity'] += 2
                body = b'{}'
            elif self.path == '/app':
                body = '''<!doctype html><meta charset="utf-8"><h1>검색 결과</h1>
<ul data-testid="search-results"><li><a href="/app/products/1">무선 키보드</a></li><li><a href="/app/products/2">마우스</a></li></ul>'''.encode()
            elif self.path == '/app/products/1':
                body = '''<!doctype html><meta charset="utf-8"><h1>무선 키보드 15,000원</h1>
<button data-testid="add-to-cart">장바구니 담기</button><p id="added"></p>
<button data-testid="go-to-cart" onclick="location.href='/app/cart'">장바구니</button>
<script>document.querySelector('[data-testid="add-to-cart"]').onclick = async () => {
  await fetch('/app/api/cart/add'); document.querySelector('#added').textContent = '담겼습니다'; };</script>'''.encode()
            else:  # /app/cart: the report page; empty until something is added
                body = f'''<!doctype html><meta charset="utf-8"><body><div>
<div><h1>장바구니</h1></div>
<div><span id="quantity">{cart['quantity']}</span><button data-testid="quantity-decrease">-</button></div>
<div><ul><li><button>WELCOME</button></li><li><button>SAVE5000</button></li></ul>
<input id="coupon"><button data-testid="apply-coupon">쿠폰 적용</button><p id="coupon-status"></p>
<p>결제 금액 <span id="total">{cart['quantity'] * 15000:,}원</span></p></div></div>
<script>
const q = document.querySelector('#quantity'), total = document.querySelector('#total');
document.querySelector('[data-testid="quantity-decrease"]').onclick = () => {{
  q.textContent = Math.max(1, Number(q.textContent) - 1);
  total.textContent = (Number(q.textContent) * 15000 + 15000).toLocaleString('en-US') + '원'; }};
document.querySelectorAll('ul button').forEach(b => b.onclick = () => document.querySelector('#coupon').value = b.textContent);
document.querySelector('[data-testid="apply-coupon"]').onclick = () => {{
  if (Number(q.textContent) < 1) return;
  document.querySelector('#coupon-status').textContent =
    document.querySelector('#coupon').value === 'SAVE5000' ? '쿠폰 확인 완료' : '쿠폰을 선택하세요'; }};
</script>'''.encode()
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


class ReplayStepsTests(unittest.TestCase):
    origin = 'https://shop.test'
    policy = RunPolicy('demo', 1, 'https://shop.test', ('/app',))

    def test_bug31_activity_replays_from_the_earliest_recorded_page(self):
        steps = replay_steps(activity(), self.origin + '/app/cart', self.policy)
        self.assertEqual([(s['action'], s['target']) for s in steps],
                         [('navigate', self.origin + '/app'), *(('click', target) for target in RECORDED)])

    def test_unsafe_or_incomplete_recordings_are_not_replayed(self):
        self.assertEqual(replay_steps(activity(truncated='true'), self.origin + '/app/cart', self.policy), ())
        narrow = RunPolicy('demo', 1, 'https://shop.test', ('/app/cart',))
        self.assertEqual(replay_steps(activity(), self.origin + '/app/cart', narrow), ())
        self.assertEqual(replay_steps('', self.origin + '/app/cart', self.policy), ())

    def test_only_grounded_model_fills_join_the_replay_before_the_matching_click(self):
        steps = replay_steps(activity(), self.origin + '/app/cart', self.policy)
        generated = model_plan(self.origin, [{'action': 'fill', 'target': '#coupon', 'value': 'SAVE5000'},
                                             *clicks('#apply')])['plan']
        composed = compose(generated, steps, self.origin + '/app/cart')
        self.assertEqual([s['action'] for s in composed['steps']][-2:], ['fill', 'click'])
        self.assertEqual(composed['ready'], generated['ready'])


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'install browser extra')
class RecordedReplayBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, origin, cart, responses, recorded):
        async def reset(_):
            cart['quantity'] = 0
            return True
        model = ScriptedModel(responses)
        with tempfile.TemporaryDirectory() as root:
            service = QAService(model, local_policy(origin), JobStore(root), options=BrowserOptions(step_timeout_ms=500))
            return await service.run(QARequest('bug31', 'demo', origin + '/app/cart', description=DESCRIPTION,
                                               reporter_activity=recorded), reset=reset, is_authorized=lambda: True)

    async def test_bug31_replays_recorded_flow_although_the_model_plan_is_invalid(self):
        with shop_site() as (origin, cart):
            # The model only acts on the (empty) report page; its checks are still what the host needs.
            invalid = model_plan(origin, clicks('[data-testid="apply-coupon"]'))
            result = await self.run_case(origin, cart, [bug31_intake(), invalid, analysis()], activity())
            self.assertEqual(result.status, 'completed', result)
            self.assertEqual(result.report['plan_validation'], [])
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
            self.assertEqual([s['target'] for s in result.report['scenario']['steps']],
                             [origin + '/app/cart', origin + '/app', *RECORDED])
            self.assertEqual(result.report['repairs'], [])

    async def test_missing_recorded_selector_falls_back_to_the_model_plan(self):
        with shop_site() as (origin, cart):
            own = model_plan(origin, [{'action': 'navigate', 'target': origin + '/app', 'value': ''},
                                      *clicks(*RECORDED)])
            data = bug31_intake()
            data['required_actions'] = []
            result = await self.run_case(origin, cart, [data, own, analysis()],
                                         activity(apply='[data-testid="apply-coupon-old"]'))
            self.assertEqual(result.status, 'completed', result)
            self.assertEqual(result.report['repairs'][0]['kind'], 'replay_fallback')
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
            self.assertEqual(result.report['scenario']['steps'][-1]['target'], '[data-testid="apply-coupon"]')


if __name__ == '__main__':
    unittest.main()
