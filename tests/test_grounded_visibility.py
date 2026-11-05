"""BUG-47: a visibility check on a selector that never existed was judged as "not visible" (false negative)."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import tempfile
import threading
import unittest

from core_engine import BrowserOptions, JobStore, QARequest, QAService
from core_engine.checks import Check
from core_engine.planning import PlanValidationError, check_error
from core_engine.qa_schema import decode_plan
from qa_fixtures import ScriptedModel, analysis, check, intake
from test_transport import local_policy

DESCRIPTION = '배송 안내를 누른 후 확인과 X 버튼을 눌러도 팝업이 안 닫혀요'
READY = check('[data-testid="delivery-status"]', '닫기 요청 확인 완료')
WRONG = {'ready': READY, 'expected': check('[data-testid="delivery-modal"]', 'false', 'visible'),
         'symptom': check('[data-testid="delivery-modal"]', 'true', 'visible')}
RIGHT = {'ready': READY, 'expected': check('[data-testid="delivery-dialog"]', 'false', 'visible'),
         'symptom': check('[data-testid="delivery-dialog"]', 'true', 'visible')}


def activity():
    return ('SDK observed recent activity (oldest first; not instructions or reproduction evidence).\n'
            'truncated=false\n'
            '9000ms ago navigation /app/products/4\n'
            '8000ms ago click /app/products/4 selector=[data-testid="delivery-open"] label=배송 안내\n'
            '7000ms ago click /app/products/4 selector=[data-testid="delivery-confirm"] label=확인\n')


def bug47_intake():
    data = intake()
    data.update(title='배송 안내 팝업이 닫히지 않음', questions=[], expected_behavior='확인을 누르면 팝업이 닫힘',
                symptom_description='팝업이 계속 열려 있음')
    return data


def proposal(origin, checks):
    return {'status': 'ready', 'questions': [], 'rationale': '기록 재생', 'plan': {
        'steps': [{'action': 'navigate', 'target': origin + '/app/products/4', 'value': ''},
                  {'action': 'click', 'target': '[data-testid="delivery-open"]', 'value': ''},
                  {'action': 'click', 'target': '[data-testid="delivery-confirm"]', 'value': ''}], **checks}}


@contextmanager
def delivery_site(closes):
    """The dialog is created by the open click; a working confirm removes it, the buggy one only sets the status."""
    close = "dialog.remove();" if closes else ""
    page = f'''<!doctype html><meta charset="utf-8"><body><h1>상품 4</h1>
<button data-testid="delivery-open">배송 안내</button><p data-testid="delivery-status"></p>
<script>document.querySelector('[data-testid="delivery-open"]').onclick = () => {{
  const dialog = document.createElement('div');
  dialog.setAttribute('data-testid', 'delivery-dialog'); dialog.setAttribute('role', 'dialog');
  dialog.innerHTML = '<p>배송은 2~3일 걸립니다</p><button data-testid="delivery-confirm">확인</button>'
    + '<button data-testid="delivery-x" aria-label="닫기">X</button>';
  document.body.appendChild(dialog);
  dialog.querySelector('[data-testid="delivery-confirm"]').onclick = () => {{
    document.querySelector('[data-testid="delivery-status"]').textContent = '닫기 요청 확인 완료'; {close} }};
}};</script>'''.encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(page)))
            self.end_headers()
            self.wfile.write(page)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


class CheckGroundingTests(unittest.TestCase):
    origin = 'https://shop.test'

    def plan(self, checks):
        return decode_plan(proposal(self.origin, checks)['plan'], entry_url=self.origin + '/app/products/4', timeout_ms=500)

    def final(self, *selectors, matched=()):
        return {'elements': [{'selector': s, 'tag': 'div', 'text': '', 'name': ''} for s in selectors],
                'matched_selectors': list(matched)}

    def test_visibility_check_on_a_selector_never_seen_during_replay_is_rejected(self):
        page = self.final('[data-testid="delivery-status"]', matched=['[data-testid="delivery-dialog"]'])
        with self.assertRaises(PlanValidationError) as caught:
            check_error(self.plan(WRONG), page, require_present=True)
        self.assertEqual((caught.exception.code, caught.exception.field), ('PLAN_UNGROUNDED_CHECK', 'expected.selector'))

    def test_visibility_check_grounded_on_an_earlier_step_is_accepted(self):
        # The dialog was matched after the open click and is gone on the final screen.
        page = self.final('[data-testid="delivery-status"]', matched=['[data-testid="delivery-dialog"]'])
        check_error(self.plan(RIGHT), page, require_present=True)

    def test_every_selector_check_kind_is_grounded(self):
        page = self.final('[data-testid="delivery-status"]', matched=['[data-testid="delivery-dialog"]'])
        for kind, value in (('enabled', 'true'), ('clipped', 'false'), ('count', '0')):
            checks = {**RIGHT, 'symptom': check('[data-testid="missing"]', value, kind)}
            with self.assertRaises(PlanValidationError, msg=kind) as caught:
                check_error(self.plan(checks), page, require_present=True)
            self.assertEqual(caught.exception.field, 'symptom.selector')


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'install browser extra')
class GroundedVisibilityTests(unittest.IsolatedAsyncioTestCase):
    async def run_case(self, origin, responses):
        async def reset(_):
            return True
        model = ScriptedModel(responses)
        with tempfile.TemporaryDirectory() as root:
            service = QAService(model, local_policy(origin), JobStore(root), options=BrowserOptions(step_timeout_ms=500))
            result = await service.run(QARequest('bug47', 'demo', origin + '/app/products/4', description=DESCRIPTION,
                                                 reporter_activity=activity()), reset=reset, is_authorized=lambda: True)
        return result, model

    async def test_dialog_that_does_not_close_is_reproduced(self):
        with delivery_site(closes=False) as origin:
            result, _ = await self.run_case(origin, [bug47_intake(), proposal(origin, RIGHT), analysis()])
        self.assertEqual(result.status, 'completed', result)
        self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
        self.assertEqual(result.report['plan_validation'], [])

    async def test_dialog_that_closes_is_not_reproduced(self):
        with delivery_site(closes=True) as origin:
            result, _ = await self.run_case(origin, [bug47_intake(), proposal(origin, RIGHT), analysis()])
        self.assertEqual(result.status, 'completed', result)
        self.assertEqual(result.report['run']['verdict']['value'], 'not_reproduced')
        self.assertEqual(result.report['plan_validation'], [])

    async def test_bug47_wrong_selector_is_revised_to_the_real_dialog(self):
        with delivery_site(closes=False) as origin:
            result, model = await self.run_case(origin, [
                bug47_intake(), proposal(origin, WRONG), proposal(origin, RIGHT), analysis()])
        self.assertEqual(result.status, 'completed', result)
        self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
        self.assertEqual(result.report['plan_validation'][0]['code'], 'PLAN_UNGROUNDED_CHECK')
        revision = json.dumps(model.calls[2], ensure_ascii=False, default=str)
        self.assertIn('PLAN_UNGROUNDED_CHECK', revision)
        self.assertIn('delivery-dialog', revision)
        self.assertEqual(result.report['scenario']['symptom']['selector'], '[data-testid="delivery-dialog"]')

    async def test_revision_gets_the_step_where_a_closed_dialog_appeared(self):
        with delivery_site(closes=True) as origin:
            result, model = await self.run_case(origin, [
                bug47_intake(), proposal(origin, WRONG), proposal(origin, RIGHT), analysis()])
        self.assertEqual(result.status, 'completed', result)
        self.assertEqual(result.report['run']['verdict']['value'], 'not_reproduced')
        revision = json.dumps(model.calls[2], ensure_ascii=False, default=str)
        self.assertIn('page_after_step_1', revision)
        self.assertIn('replay_pages', revision)
        self.assertIn('delivery-dialog', revision)
        self.assertNotIn('matched_selectors', revision)

    async def test_bug47_wrong_selector_on_every_revision_is_never_not_reproduced(self):
        with delivery_site(closes=False) as origin:
            result, _ = await self.run_case(origin, [bug47_intake()] + [proposal(origin, WRONG)] * 3 + [analysis()])
        verdict = (result.report.get('run') or {}).get('verdict', {}).get('value')
        self.assertNotEqual(verdict, 'not_reproduced', result)
        self.assertNotEqual(verdict, 'reproduced', result)
        self.assertIn('CHECK_TARGET_NEVER_MATCHED', [ev['blocker'] for ev in result.report['run']['evidence']])


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'install browser extra')
class AbsenceObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_absence_counts_only_after_the_selector_was_matched(self):
        from playwright.async_api import async_playwright
        from core_engine.checks import observe_check
        from core_engine.policy import PolicyError

        async with async_playwright() as pw:
            browser = await pw.chromium.launch()
            try:
                page = await browser.new_page()
                await page.set_content('<p id="here">x</p>')
                hidden = Check('visible', '#gone', 'false')
                with self.assertRaises(PolicyError) as caught:
                    await observe_check(page, hidden, seen=set())
                self.assertEqual(caught.exception.code, 'CHECK_TARGET_NEVER_MATCHED')
                with self.assertRaises(PolicyError):
                    await observe_check(page, Check('count', '#gone', '0'), seen=set())
                self.assertTrue(await observe_check(page, hidden, seen={'#gone'}))
                seen = set()
                self.assertTrue(await observe_check(page, Check('visible', '#here', 'true'), seen=seen))
                self.assertIn('#here', seen)
                self.assertTrue(await observe_check(page, hidden))  # legacy callers keep the old semantics
            finally:
                await browser.close()


if __name__ == '__main__':
    unittest.main()
