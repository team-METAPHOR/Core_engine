"""실제 브라우저와 모의 모델로 전달·근거 인용·재실행을 검증합니다. AI 정확도 평가는 아닙니다."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest

from core_engine import BrowserOptions, JobStore, QARequest, QAService
from qa_fixtures import ScriptedModel, analysis, check, intake
from test_transport import local_policy


@contextmanager
def cart_site():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            body = b'''<!doctype html><meta charset="utf-8">
<button data-testid="add" onclick="history.pushState({},'', '/app/cart')">Add to cart</button>
<button data-testid="increase" onclick="document.querySelector('#status').textContent='checked'">+</button>
<p id="quantity">1</p><p id="status" role="status"></p>'''
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


async def reset(_):
    return True


class ActivityFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_report_context_reaches_intake_plan_critic_and_retest(self):
        with cart_site() as origin, tempfile.TemporaryDirectory() as root:
            context = ('SDK observed recent activity (oldest first). truncated=false\n'
                       '2000ms ago navigation /app/product\n'
                       '1500ms ago click /app/product selector=[data-testid="add"] label=Add to cart\n'
                       '1000ms ago navigation /app/cart\n'
                       '500ms ago click /app/cart selector=[data-testid="increase"] label=+ '
                       'post-click feedback=checked')
            interpreted = intake()
            interpreted.update(title='수량 늘리기 무반응', expected_behavior='수량 증가',
                symptom_description='수량이 그대로', preconditions=[],
                required_actions=[
                    {'action': 'navigate', 'value': '/app/product', 'source_quote': 'navigation /app/product'},
                    {'action': 'click', 'value': '', 'source_quote': 'selector=[data-testid="add"] label=Add to cart'},
                    {'action': 'click', 'value': '', 'source_quote': 'selector=[data-testid="increase"] label=+'}],
                actions_complete=True,
                completion_signal={'value': 'checked', 'source_quote': 'post-click feedback=checked'})
            proposal = {'status': 'ready', 'questions': [], 'rationale': '기록의 준비 경로와 두 클릭 보존',
                'plan': {'steps': [
                    {'action': 'navigate', 'target': origin + '/app/product', 'value': ''},
                    {'action': 'click', 'target': '[data-testid="add"]', 'value': ''},
                    {'action': 'click', 'target': '[data-testid="increase"]', 'value': ''}],
                    'ready': check('#status', 'checked'), 'expected': check('#quantity', '2'),
                    'symptom': check('#quantity', '1')}}
            model = ScriptedModel([interpreted, proposal, analysis(), analysis()])
            critic = ScriptedModel([{'assessment': {'decision': 'accept', 'summary': '기록과 일치'}}])
            store = JobStore(Path(root))
            service = QAService(model, local_policy(origin), store, reviewer=critic,
                                options=BrowserOptions(step_timeout_ms=300))
            result = await service.run(QARequest('activity-report', 'demo', origin + '/app/cart',
                description='수량 늘리기를 눌러도 수량이 늘지 않아요.', reporter_activity=context),
                reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'completed', result)
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
            self.assertEqual(result.questions, ())
            self.assertEqual([s['target'] for s in result.report['scenario']['steps'][:2]],
                             [origin + '/app/cart', origin + '/app/product'])
            for call in [*model.calls[:2], *critic.calls]:
                report = call['context'].get('report', call['context'])
                self.assertEqual(report['reporter_activity'], context)
            self.assertEqual(store.read_json(result.job_id, 'request.json')['reporter_activity'], context)
            retest = await service.retest(result.job_id, reset=reset, is_authorized=lambda: True)
            self.assertEqual(retest.report['run']['verdict']['value'], 'reproduced')
