"""Opt-in real BugLab B02 integration. No model, existing DB, .env or production service."""

import asyncio
from contextlib import contextmanager
from dataclasses import asdict
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from urllib.request import Request, urlopen

from core_engine import (BrowserOptions, BrowserPlan, BrowserRunner, BudgetLimits, Check,
    LoginOptions, RunPolicy, StateChangePolicy, StateReset, Step, TextCheck)
from core_engine.evaluation.local_tls import local_https


@contextmanager
def isolated_buglab(root):
    if not (root / 'dist/index.html').is_file():
        raise RuntimeError('Build the isolated BugLab checkout first: npm ci; npm run build; npm --prefix server ci')
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0)); port = s.getsockname()[1]
    password, token = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    hits, resets = [], []
    with tempfile.TemporaryDirectory(prefix='buglab-engine-') as temporary:
        env = {**os.environ, 'APP_ENV': 'dev', 'HOST': '127.0.0.1', 'PORT': str(port),
            'DATA_DIR': temporary, 'STATIC_DIR': str(root / 'dist'), 'BUILD_COMMIT': root.name,
            'TEST_ACCOUNT_PASSWORD': password, 'TEST_CONTROL_TOKEN': token, 'COOKIE_SECURE': 'true',
            'METAPHOR_SDK_URL': '', 'METAPHOR_API_URL': '', 'METAPHOR_PROJECT_KEY': ''}
        with open(Path(temporary) / 'server.log', 'w') as log:
            process = subprocess.Popen(['node', 'server/src/index.js'], cwd=root, env=env, stdout=log, stderr=log)
            try:
                for _ in range(60):
                    if process.poll() is not None:
                        raise RuntimeError('Isolated BugLab server exited')
                    try:
                        with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=.5) as response:
                            assert json.load(response)['env'] == 'dev'
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('Isolated BugLab did not start')

                class Proxy(BaseHTTPRequestHandler):
                    def log_message(self, *_): pass

                    def forward(self):
                        upstream = http.client.HTTPConnection('127.0.0.1', port, timeout=5)
                        try:
                            body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                            upstream.request(self.command, self.path, body=body, headers=dict(self.headers))
                            response = upstream.getresponse()
                            payload = response.read()
                            hits.append((self.command, self.path.split('?')[0], response.status))
                            self.send_response(response.status)
                            for key, value in response.getheaders():
                                if key.lower() not in {'transfer-encoding', 'connection', 'content-length'}:
                                    self.send_header(key, value)
                            self.send_header('Content-Length', str(len(payload))); self.end_headers()
                            self.wfile.write(payload)
                        finally:
                            upstream.close()
                    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = forward

                proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
                with local_https(proxy):
                    thread = threading.Thread(target=lambda: proxy.serve_forever(poll_interval=.01), daemon=True)
                    thread.start()
                    origin = f'https://127.0.0.1:{proxy.server_port}'
                    async def reset(n):
                        def call():
                            request = Request(origin + '/api/test/reset', method='POST',
                                data=json.dumps({'email': 'qa1@buglab.test'}).encode(),
                                headers={'Content-Type': 'application/json', 'X-Test-Token': token})
                            with urlopen(request, timeout=5, context=ssl.create_default_context()) as response:
                                return response.status == 200
                        success = await asyncio.to_thread(call)
                        if success: resets.append(n)
                        return success
                    try:
                        yield origin, password, reset, hits, resets
                    finally:
                        proxy.shutdown(); proxy.server_close(); thread.join()
            finally:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait()


@unittest.skipUnless(os.environ.get('BUGLAB_CHECKOUT'), 'set BUGLAB_CHECKOUT to a built isolated BugLab checkout')
class BugLabIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_anonymous_initial_401_preserves_normal_sold_out_control(self):
        with isolated_buglab(Path(os.environ['BUGLAB_CHECKOUT']).resolve()) as (origin, _, reset, hits, _):
            policy = RunPolicy('buglab', 1, origin, ('/',), allow_private_targets=True, private_cidrs=('127.0.0.1/32',))
            plan = BrowserPlan((Step('navigate', origin + '/products/3'),),
                ready=Check('visible', '[data-testid=add-to-cart]', 'true'),
                symptom=Check('enabled', '[data-testid=add-to-cart]', 'true'),
                expected=Check('enabled', '[data-testid=add-to-cart]', 'false'))
            result = await BrowserRunner(policy, BudgetLimits(max_requests_per_minute=120, max_total_requests=300)).run(
                plan, reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.value, 'not_reproduced', result)
            self.assertEqual([h for h in hits if h[1] == '/api/auth/me'], [('GET', '/api/auth/me', 401)] * 2)

    async def test_b02_two_independent_runs(self):
        with isolated_buglab(Path(os.environ['BUGLAB_CHECKOUT']).resolve()) as (origin, password, reset, hits, resets):
            changes = StateChangePolicy('test', 'buglab-reset', (
                ('POST', '/api/cart/items'), ('PATCH', '/api/cart/items')))
            policy = RunPolicy('buglab', 1, origin, ('/',), excluded_path_prefixes=('/api/test', '/api/auth/signup'),
                allow_private_targets=True, private_cidrs=('127.0.0.1/32',), state_changes=changes)
            options = BrowserOptions(width=1440, height=1000, test_context_id='buglab-qa1',
                login=LoginOptions('/login', 'qa1@buglab.test', password,
                    submit_path='/api/auth/login', username_selector='[data-testid=login-email]',
                    password_selector='[data-testid=login-password]', submit_selector='[data-testid=login-submit]',
                    success_selector='[data-testid=user-menu]', unauthenticated_paths=('/api/auth/me',)))
            plan = BrowserPlan((Step('navigate', origin + '/products/2'), Step('click', '[data-testid=add-to-cart]'),
                Step('assert_text', '[data-testid=add-to-cart-message]', '장바구니에 담았습니다.'),
                Step('navigate', origin + '/cart'), Step('click', '[data-testid=quantity-increase]'),
                Step('assert_text', '[data-testid=cart-quantity]', '2'),
                Step('assert_text', '[data-testid=cart-line-total]', '36,000원'),
                Step('click', '[data-testid=quantity-decrease]')),
                ready=TextCheck('[data-testid=cart-quantity]', '1'),
                symptom=TextCheck('[data-testid=cart-subtotal]', '36,000원'),
                expected=TextCheck('[data-testid=cart-subtotal]', '18,000원'))
            runner = BrowserRunner(policy, BudgetLimits(max_requests_per_minute=120, max_total_requests=300), options=options)
            result = await runner.run(plan, reset=StateReset('buglab-reset', reset), is_authorized=lambda: True)
            print(json.dumps({'case': 'BugLab B02', 'verdict': result.verdict.value,
                'blockers': [e.blocker for e in result.evidence], 'resets': resets,
                'request_count': len(hits), 'http_error_statuses': [h for h in hits if h[2] >= 400],
                'state_changing_requests': [e.state_changing_requests for e in result.evidence]}, ensure_ascii=False))
            self.assertEqual(result.verdict.value, 'reproduced', result)
            self.assertEqual(resets, [1, 2])
            self.assertEqual(sum(m == 'PATCH' for m, _, _ in hits), 4)
            self.assertNotIn(password, json.dumps(asdict(result)))
