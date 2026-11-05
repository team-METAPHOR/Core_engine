"""BUG-41: static assets do not consume the request budget; rate-window waits pause the step clock."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import threading
import time
import unittest
from unittest.mock import patch

from core_engine import BrowserPlan, BrowserRunner, BudgetLimits, PolicyError, RequestBudget, Step, TextCheck
from core_engine.transport import GuardedTransport
from test_transport import local_policy

PNG = b'\x89PNG\r\n\x1a\n' + b'\0' * 92


@contextmanager
def gallery_site():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            hits.append(self.path)
            content_type = 'text/html; charset=utf-8'
            if self.path.startswith('/app/img/'):
                body, content_type = PNG, 'image/png'
            elif self.path == '/app/gallery':
                images = ''.join(f'<img src="/app/img/{i}.png">' for i in range(30))
                body = ('<!doctype html><p id="status">idle</p><p id="result">0</p>' + images +
                        '<button id="buy" onclick="status.textContent=\'done\';result.textContent=\'2\'">buy</button>').encode()
            elif self.path == '/app/api/product':
                body, content_type = b'{"name":"keyboard"}', 'application/json'
            elif self.path == '/app/lazy':
                # The product renders only after an API call (SPA "loading..." state).
                body = '''<!doctype html><p id="status">상품 정보를 불러오는 중입니다.</p><p id="result">0</p>
<script>fetch('/app/api/product').then(r => r.json()).then(() => {
  const b = document.createElement('button'); b.id = 'buy'; b.textContent = 'buy';
  b.onclick = () => { status.textContent = 'done'; result.textContent = '2'; };
  document.body.append(b); status.textContent = 'loaded'; });</script>'''.encode()
            else:
                body = b'missing'
            self.send_response(200 if body != b'missing' else 404)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', hits
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def plan(origin, path):
    return BrowserPlan((Step('navigate', origin + path), Step('click', '#buy')),
                       TextCheck('#status', 'done'), TextCheck('#result', '2'), step_timeout_ms=500,
                       expected=TextCheck('#result', '1'))


async def reset(_):
    return True


class BudgetUnitTests(unittest.TestCase):
    def test_uncounted_requests_skip_rate_and_total_but_keep_concurrency(self):
        budget = RequestBudget(BudgetLimits(max_requests_per_minute=1, max_total_requests=1, max_concurrent_requests=1))
        with budget.request():
            with self.assertRaises(PolicyError) as error, budget.request(counted=False):
                pass
            self.assertEqual(error.exception.code, 'CONCURRENCY_LIMIT')
        for _ in range(5):
            with budget.request(counted=False):
                pass
        self.assertEqual(budget.total, 1)
        with self.assertRaises(PolicyError), budget.request():
            pass

    def test_rate_window_wait_is_accounted(self):
        with patch('core_engine.budget.RATE_WINDOW_SECONDS', 0.3):
            budget = RequestBudget(BudgetLimits(max_requests_per_minute=1))
            with budget.request(wait=True):
                pass
            self.assertEqual(budget.rate_waited_seconds(), 0)
            started = time.monotonic()
            with budget.request(wait=True):
                pass
            waited = budget.rate_waited_seconds()
            self.assertGreater(waited, 0.2)
            self.assertLessEqual(waited, time.monotonic() - started + 0.01)

    def test_transport_counts_documents_and_api_but_not_same_origin_static_assets(self):
        with gallery_site() as (origin, _):
            budget = RequestBudget(BudgetLimits(max_requests_per_minute=2))
            transport = GuardedTransport(local_policy(origin), budget, lambda: True, threading.Event())
            for _ in range(5):
                transport.fetch(origin + '/app/img/1.png', resource_type='image')
            transport.fetch(origin + '/app/gallery', resource_type='document')
            transport.fetch(origin + '/app/api/product', resource_type='fetch')
            self.assertEqual(budget.total, 2)
            self.assertGreaterEqual(budget.downloaded, 5 * len(PNG))


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'install browser extra')
class StaticBudgetBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_heavy_page_does_not_exhaust_request_budget(self):
        with gallery_site() as (origin, hits):
            limits = BudgetLimits(max_requests_per_minute=5, max_total_requests=10)
            budget = RequestBudget(limits)
            result = await BrowserRunner(local_policy(origin), limits).run(
                plan(origin, '/app/gallery'), reset=reset, is_authorized=lambda: True, budget=budget)
            self.assertEqual(result.verdict.value, 'reproduced', result)
            self.assertGreaterEqual(sum(p.startswith('/app/img/') for p in hits), 60)
            self.assertEqual(budget.total, 2)

    async def test_static_assets_still_obey_download_limit(self):
        with gallery_site() as (origin, _):
            limits = BudgetLimits(max_download_bytes=2048, max_response_bytes=2048)
            result = await BrowserRunner(local_policy(origin), limits).run(
                plan(origin, '/app/gallery'), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.value, 'inconclusive')
            self.assertTrue(all(a.blocker == 'DOWNLOAD_LIMIT' for a in result.attempts), result.attempts)

    async def test_step_clock_pauses_while_api_waits_for_rate_window(self):
        # An earlier replay pass already used the window: the SPA's API call is paced (not failed)
        # far beyond the 500 ms step timeout, and the click must wait for the product to render.
        with patch('core_engine.budget.RATE_WINDOW_SECONDS', 4.0), gallery_site() as (origin, _):
            limits = BudgetLimits(max_requests_per_minute=2)
            budget = RequestBudget(limits)
            with budget.request():
                pass
            result = await BrowserRunner(local_policy(origin), limits).run(
                plan(origin, '/app/lazy'), reset=reset, is_authorized=lambda: True, budget=budget)
            self.assertEqual(result.verdict.value, 'reproduced', result)
            self.assertGreater(budget.rate_waited_seconds(), 1.0)


if __name__ == '__main__':
    unittest.main()
