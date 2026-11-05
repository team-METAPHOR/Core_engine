from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import tempfile
import time
import unittest
from unittest.mock import patch

from core_engine import (BrowserOptions, BrowserPlan, BrowserRunner, BudgetLimits, JobStore, PolicyError, Recorder, RequestBudget,
                         RunPolicy, StateChangePolicy, StateReset, Step, TextCheck)
from core_engine.evaluation.local_tls import local_https
from core_engine.transport import GuardedTransport


@contextmanager
def browser_site():
    state = {'active': 0, 'peak': 0, 'hits': [], 'quantity': 1, 'total': 18000, 'round': 0, 'fixed': False}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, payload, content_type='text/html', status=200, cookie=None):
            body = payload.encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            if cookie:
                self.send_header('Set-Cookie', cookie)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state['hits'].append(('GET', self.path))
            if self.path.startswith('/app/asset-'):
                with lock:
                    state['active'] += 1
                    state['peak'] = max(state['peak'], state['active'])
                time.sleep(.12)
                with lock:
                    state['active'] -= 1
                self.send("window.loaded=(window.loaded||0)+1;document.querySelector('#result').textContent=loaded;", 'application/javascript')
            elif self.path == '/app/slow':
                time.sleep(1.15)
                self.send('slow response')
            elif self.path == '/app/assets':
                tags = ''.join(f'<script defer src="/app/asset-{i}.js"></script>' for i in range(8))
                self.send('<!doctype html><div id="result">0</div><div id="ready">done</div>' + tags)
            elif self.path == '/app/external':
                outside = f'https://localhost:{self.server.server_port}'
                self.send(f'''<!doctype html><script src="{outside}/sdk.js"></script>
<link rel="stylesheet" href="{outside}/font.css"><div id="ready">loading</div><div id="result">broken</div>
<script>fetch('{outside}/api/config').catch(()=>{{}}).finally(()=>document.querySelector('#ready').textContent='done')</script>''')
            elif self.path == '/app/cart':
                self.send('''<!doctype html><div id="ready">idle</div><div id="quantity">1</div><div id="result">18000</div>
<button id="increase" onclick="change(2)">Increase</button><button id="decrease" onclick="change(1)">Decrease</button>
<script>async function change(quantity) {
document.querySelector('#ready').textContent='working';
const r=await fetch('/app/api/cart/items/2',{method:'PATCH',headers:{'Content-Type':'application/json','X-CSRF-Token':'synthetic-csrf'},body:JSON.stringify({quantity})});
const data=await r.json();document.querySelector('#quantity').textContent=data.quantity;
document.querySelector('#result').textContent=data.total;document.querySelector('#ready').textContent='done';
}</script>''', cookie=f'run=synthetic-{state["round"]}; Path=/app; Secure; HttpOnly')
            elif self.path.startswith('/app/api/error-'):
                self.send('{}', 'application/json', int(self.path.rsplit('-', 1)[1]))
            elif self.path.startswith('/app/client-error-'):
                code = int(self.path.rsplit('-', 1)[1])
                self.send(f'''<!doctype html><div id="ready">loading</div><div id="result">normal</div>
<script>fetch('/app/api/error-{code}').then(()=>document.querySelector('#ready').textContent='done');</script>''')
            elif self.path == '/app/document-500':
                self.send('unavailable', status=500)
            else:
                self.send('missing', status=404)

        def do_PATCH(self):
            state['hits'].append(('PATCH', self.path))
            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
            if self.path == '/app/api/cart/items/redirect':
                self.send_response(307)
                self.send_header('Location', '/app/api/cart/items/2')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            if self.path != '/app/api/cart/items/2' or self.headers.get('Cookie') != f'run=synthetic-{state["round"]}' or self.headers.get('X-CSRF-Token') != 'synthetic-csrf':
                self.send('{}', 'application/json', 403)
                return
            state['quantity'] = json.loads(body)['quantity']
            if state['quantity'] == 2 or state['fixed']:
                state['total'] = state['quantity'] * 18000
            self.send(json.dumps({k: state[k] for k in ('quantity', 'total')}), 'application/json')

        def do_POST(self):
            if self.path == '/reset':
                state.update(quantity=1, total=18000, round=state['round'] + 1)
                self.send('{}', 'application/json')
            else:
                self.send('{}', 'application/json', 404)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    with local_https(server):
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        try:
            yield f'https://127.0.0.1:{server.server_port}', state
        finally:
            server.shutdown(); server.server_close(); thread.join()


def approved(origin, *, writes=False):
    return RunPolicy('demo', 1, origin, ('/app',), allow_private_targets=True, private_cidrs=('127.0.0.1/32',),
        state_changes=StateChangePolicy('test', 'cart-reset', (('PATCH', '/app/api/cart/items'),)) if writes else None)


def cart_plan(origin):
    return BrowserPlan((Step('navigate', origin + '/app/cart'), Step('click', '#increase'),
        Step('assert_text', '#result', '36000'), Step('click', '#decrease'), Step('assert_text', '#quantity', '1')),
        ready=TextCheck('#ready', 'done'), symptom=TextCheck('#result', '36000'), expected=TextCheck('#result', '18000'))


class QueueTests(unittest.TestCase):
    def test_write_redirect_does_not_resend_body(self):
        with browser_site() as (origin, state):
            transport = GuardedTransport(approved(origin, writes=True), RequestBudget(BudgetLimits()),
                lambda: True, threading.Event())
            with self.assertRaises(PolicyError) as error:
                transport.fetch(origin + '/app/api/cart/items/redirect', 'PATCH',
                    browser_headers={'Content-Type': 'application/json'}, request_body=b'{"quantity":2}')
            self.assertEqual(error.exception.code, 'STATE_CHANGE_REDIRECT_UNSUPPORTED')
            self.assertEqual(state['hits'], [('PATCH', '/app/api/cart/items/redirect')])

    def test_write_body_limits_are_checked_before_network(self):
        transport = GuardedTransport(approved('https://example.test', writes=True),
            RequestBudget(BudgetLimits()), lambda: True, threading.Event())
        for headers, body in (({'Content-Type': 'text/plain'}, b'private'),
                              ({'Content-Type': 'application/json'}, b'x' * 16385)):
            with patch.object(transport, 'resolver') as resolver, self.assertRaises(PolicyError):
                transport.fetch('https://example.test/app/api/cart/items/2', 'PATCH',
                    browser_headers=headers, request_body=body)
            resolver.assert_not_called()

    def test_queue_bounds_real_concurrency(self):
        budget = RequestBudget(BudgetLimits(max_concurrent_requests=2))
        barrier = threading.Barrier(8)
        peak = []
        def run(_):
            barrier.wait(timeout=2)
            with budget.request(wait=True):
                peak.append(budget.active)
                time.sleep(.03)
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(run, range(8)))
        self.assertEqual(max(peak), 2)
        self.assertEqual((budget.total, budget.active), (8, 0))

    def test_waiting_requests_check_cancel_revoke_and_runtime_before_dns(self):
        for reason in ('CANCELLED', 'AUTHORIZATION_REVOKED', 'RUNTIME_LIMIT'):
            with self.subTest(reason=reason):
                clock, authorized = [0.0], [True]
                budget = RequestBudget(BudgetLimits(max_concurrent_requests=1), clock=lambda: clock[0])
                cancelled = threading.Event()
                transport = GuardedTransport(approved('https://example.test'), budget, lambda: authorized[0], cancelled)
                with ThreadPoolExecutor(max_workers=1) as executor, budget.request(), patch.object(transport, 'resolver') as resolver:
                    future = executor.submit(transport.fetch, 'https://example.test/app')
                    if reason == 'CANCELLED': cancelled.set()
                    if reason == 'AUTHORIZATION_REVOKED': authorized[0] = False
                    if reason == 'RUNTIME_LIMIT': clock[0] = 45
                    with self.assertRaises(PolicyError) as error:
                        future.result(timeout=1)
                    self.assertEqual(error.exception.code, reason)
                    resolver.assert_not_called()
                self.assertEqual(budget.total, 1)

    def test_write_approval_cannot_escape_scope_or_be_enabled_by_method_only(self):
        policy = approved('https://example.test', writes=True)
        for method, path in (('POST', '/app/api/cart/items'), ('PATCH', '/app/api/cart/items-extra'), ('PATCH', '/outside')):
            with self.subTest(path=path), self.assertRaises(PolicyError):
                policy.check_url('https://example.test' + path, method)
        with self.assertRaises(PolicyError):
            replace(policy, allowed_methods=('GET', 'POST'))
        with self.assertRaises(PolicyError):
            replace(policy.state_changes, environment='production')
        with self.assertRaises(PolicyError):
            replace(policy.state_changes, reset_adapter_id='stateless')
        with self.assertRaises(PolicyError):
            replace(policy, excluded_path_prefixes=('/app/api/cart/items/2',))


class ScheduledBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_subresource_4xx_is_evidence_but_documents_and_5xx_still_block(self):
        with browser_site() as (origin, _), tempfile.TemporaryDirectory() as temporary:
            async def reset(_): return True
            for status in (401, 403, 404, 409, 500):
                with self.subTest(status=status):
                    options = BrowserOptions()
                    store = JobStore(temporary)
                    recorder = Recorder(store, store.create(), options)
                    runner = BrowserRunner(approved(origin), options=options, recorder=recorder)
                    plan = BrowserPlan((Step('navigate', origin + '/app/client-error-' + str(status)),),
                        TextCheck('#ready', 'done'), TextCheck('#result', 'broken'), expected=TextCheck('#result', 'normal'))
                    result = await runner.run(plan, reset=reset, is_authorized=lambda: True)
                    self.assertEqual(result.verdict.value, 'inconclusive' if status == 500 else 'not_reproduced', result)
                    self.assertTrue(any(r['status'] == status for s in recorder.snapshots for r in s['network']))
            for path in ('/app/missing', '/app/document-500'):
                plan = replace(plan, steps=(Step('navigate', origin + path),))
                result = await runner.run(plan, reset=reset, is_authorized=lambda: True)
                self.assertEqual(result.verdict.value, 'inconclusive')

    async def test_eight_assets_load_under_two_connections(self):
        with browser_site() as (origin, state):
            async def reset(_): return True
            plan = BrowserPlan((Step('navigate', origin + '/app/assets'),),
                TextCheck('#ready', 'done'), TextCheck('#result', '8'), expected=TextCheck('#result', 'missing'))
            result = await BrowserRunner(approved(origin), BudgetLimits(max_requests_per_minute=60)).run(plan, reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.value, 'reproduced', result)
            self.assertEqual(state['peak'], 2)
            self.assertEqual(sum('/asset-' in p for _, p in state['hits']), 16)

    async def test_blocked_sdk_css_and_config_do_not_abort_or_send(self):
        with browser_site() as (origin, state):
            async def reset(_): return True
            plan = BrowserPlan((Step('navigate', origin + '/app/external'),),
                TextCheck('#ready', 'done'), TextCheck('#result', 'broken'), expected=TextCheck('#result', 'normal'))
            result = await BrowserRunner(approved(origin)).run(plan, reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.value, 'reproduced', result)
            self.assertEqual(state['hits'], [('GET', '/app/external')] * 2)
            for evidence in result.evidence:
                self.assertEqual(len(evidence.blocked_requests), 3)
                self.assertTrue(all(not r['send_attempted'] for r in evidence.blocked_requests))
            outside = replace(plan, steps=(Step('navigate', origin + '/outside'),))
            with self.assertRaises(PolicyError):
                await BrowserRunner(approved(origin)).run(outside, reset=reset, is_authorized=lambda: True)

    async def test_state_changes_require_reset_and_preserve_body_cookie_and_csrf(self):
        with browser_site() as (origin, state):
            resets = []
            async def reset(n):
                resets.append(n)
                state.update(quantity=1, total=18000, round=state['round'] + 1)
                return True
            runner = BrowserRunner(approved(origin, writes=True), BudgetLimits(max_requests_per_minute=60))
            missing = await runner.run(cart_plan(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(missing.attempts[0].blocker, 'STATE_RESET_REQUIRED')
            self.assertEqual(state['hits'], [])
            for fixed, expected in ((False, 'reproduced'), (True, 'not_reproduced')):
                state['fixed'] = fixed
                result = await runner.run(cart_plan(origin), reset=StateReset('cart-reset', reset), is_authorized=lambda: True)
                self.assertEqual(result.verdict.value, expected, result)
                for e in result.evidence:
                    self.assertEqual(len(e.state_changing_requests), 2)
                    self.assertTrue(all(r['send_attempted'] and r['status'] == 200 for r in e.state_changing_requests))
                self.assertNotIn('synthetic-', json.dumps(asdict(result)))
            self.assertEqual(resets, [1, 2, 1, 2])

    async def test_unapproved_write_and_failed_reset_send_no_writes(self):
        with browser_site() as (origin, state):
            async def reset(_): return True
            result = await BrowserRunner(approved(origin)).run(cart_plan(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.value, 'inconclusive')
            self.assertTrue(all(a.blocker == 'METHOD_NOT_ALLOWED' for a in result.attempts))
            self.assertFalse(any(m == 'PATCH' for m, _ in state['hits']))
            state['hits'].clear()
            async def failed(_): return False
            result = await BrowserRunner(approved(origin, writes=True)).run(cart_plan(origin),
                reset=StateReset('cart-reset', failed), is_authorized=lambda: True)
            self.assertEqual(result.attempts[0].blocker, 'RESET_FAILED')
            self.assertEqual(state['hits'], [])

    async def test_transport_accepts_response_after_old_one_second_limit(self):
        with browser_site() as (origin, _):
            transport = GuardedTransport(approved(origin), RequestBudget(BudgetLimits()), lambda: True, threading.Event())
            self.assertEqual(transport.fetch(origin + '/app/slow').body, b'slow response')
