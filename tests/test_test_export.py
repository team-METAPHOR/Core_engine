import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from core_engine import BrowserOptions, BrowserPlan, Check, Step, export_playwright_test
from core_engine.qa_schema import decode_plan
from core_engine.test_export import required_test_environment
from core_engine.evaluation.condition_site import condition_site, response_plan
from fixture_site import site
from qa_fixtures import plan
from test_transport import local_policy


JS = Path(__file__).parent / 'js'


class ExportTests(unittest.TestCase):
    def test_secrets_are_not_embedded_and_query_requires_environment(self):
        scenario = decode_plan(plan('https://example.test/app?q=private-value')['plan'], timeout_ms=1000)
        opts = BrowserOptions(test_context_id='staff', session_headers={'Authorization': 'Bearer test-secret-value'})
        source = export_playwright_test(scenario, policy=local_policy('https://example.test'), options=opts)
        self.assertNotIn('test-secret-value', source)
        self.assertNotIn('private-value', source)
        self.assertIn('QA_ENTRY_URL', required_test_environment(scenario, opts))
        self.assertIn('QA_RESET_MODULE', source)
        self.assertIn('QA_STORAGE_STATE', source)

    def test_only_valid_scoped_declarative_plans_are_exported(self):
        scenario = decode_plan(plan('https://example.test/outside')['plan'], timeout_ms=1000)
        with self.assertRaises(Exception):
            export_playwright_test(scenario, policy=local_policy('https://example.test'))
        scenario = decode_plan(plan('https://example.test/app')['plan'], timeout_ms=1000)
        with self.assertRaises(ValueError):
            export_playwright_test(replace(scenario, expected=None), policy=local_policy('https://example.test'))


@unittest.skipUnless(shutil.which('node') and (JS / 'node_modules/@playwright/test/cli.js').is_file(),
                     'run npm ci --prefix tests/js for real TypeScript export verification')
class ExecutedExportTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_and_aria_export_same_test_fails_bug_passes_fixed(self):
        from test_history_actions import history_site, history_plan
        with history_site() as (origin, state):
            for action in ('reload', 'back'):
                source = export_playwright_test(history_plan(origin, action), policy=local_policy(origin))
                for fixed in (False, True):
                    state['fixed'] = fixed
                    code, report = await self.execute(source)
                    self.assertEqual(code, 0 if fixed else 1, report)
                    self.assertIn('observed-outcomes', json.dumps(report))

    async def test_url_path_export_supports_full_document_back(self):
        from test_history_actions import history_site
        with history_site() as (origin, _):
            scenario = BrowserPlan((Step('navigate', origin + '/app/history'),
                Step('navigate', origin + '/app/other'), Step('back', '')),
                Check('text', '#ready', 'loaded'), Check('url_path', '', '/app/other'),
                expected=Check('url_path', '', '/app/history'))
            code, report = await self.execute(export_playwright_test(scenario, policy=local_policy(origin)))
            self.assertEqual(code, 0, report)

    async def test_export_without_history_reports_unavailable(self):
        from test_history_actions import history_site, history_plan
        with history_site() as (origin, _):
            scenario = replace(history_plan(origin, 'back'), steps=(Step('navigate', origin + '/app/history'), Step('back', '')))
            code, report = await self.execute(export_playwright_test(scenario, policy=local_policy(origin)))
            self.assertEqual(code, 1, report)
            self.assertIn('HISTORY_UNAVAILABLE', json.dumps(report))

    async def test_approved_writes_export_uses_named_reset_and_observes_bug_and_fix(self):
        from test_request_scheduling import browser_site, approved, cart_plan
        with browser_site() as (origin, state), tempfile.TemporaryDirectory() as temporary:
            module = Path(temporary) / 'reset.mjs'
            module.write_text('export const adapterId="cart-reset"; export async function reset() { '
                + 'const response=await fetch(' + json.dumps(origin + '/reset') + ',{method:"POST"}); return response.ok; }')
            source = export_playwright_test(cart_plan(origin), policy=approved(origin, writes=True))
            code, missing = await self.execute(source)
            self.assertEqual(code, 1)
            self.assertIn('STATE_RESET_REQUIRED', json.dumps(missing))
            self.assertEqual(state['hits'], [])
            env = {'QA_RESET_MODULE': str(module), 'QA_ALLOW_STATE_CHANGES': 'cart-reset'}
            code, buggy = await self.execute(source, extra_env=env)
            self.assertEqual(code, 1, buggy)
            self.assertIn('observed-outcomes', json.dumps(buggy))
            state['fixed'] = True
            code, fixed = await self.execute(source, extra_env=env)
            self.assertEqual(code, 0, fixed)
            self.assertEqual(state['round'], 4)

    async def execute(self, source, *, extra_env=None):
        with tempfile.TemporaryDirectory(prefix='export-', dir=JS) as temporary:
            root = Path(temporary)
            (root / 'scenario.spec.ts').write_text(source)
            env = {key: value for key, value in os.environ.items() if not key.startswith('QA_')}
            env.update({'QA_RESET_MODULE': str((JS / 'reset.mjs').resolve()), **(extra_env or {})})
            # Trust exactly the temporary loopback certificate in this test browser.
            # Production exports never disable certificate validation.
            args = ['--ignore-certificate-errors-spki-list=' + env['METAPHOR_TEST_TLS_SPKI']] if env.get('METAPHOR_TEST_TLS_SPKI') else []
            config = root / 'playwright.config.mjs'
            config.write_text('export default ' + json.dumps({'testDir': str(root), 'use': {'launchOptions': {'args': args}}}))
            process = await asyncio.create_subprocess_exec('node', str(JS / 'node_modules/@playwright/test/cli.js'),
                'test', str(root / 'scenario.spec.ts'), '--reporter=json', '--workers=1', '--output=' + str(root / 'results'),
                '--config=' + str(config),
                cwd=JS, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(process.communicate(), 120)
            try:
                report = json.loads(stdout)
            except ValueError:
                self.fail(f'Playwright did not return JSON: {stdout[-3000:]!r} {stderr[-1000:]!r}')
            self.assertFalse(report.get('errors'), report)
            return process.returncode, report

    async def test_same_export_fails_bug_then_passes_fixed_without_regeneration(self):
        with site() as (origin, _, state):
            scenario = decode_plan(plan(origin + '/app/mixed')['plan'], timeout_ms=1500)
            source = export_playwright_test(scenario, policy=local_policy(origin))
            code, buggy = await self.execute(source)
            self.assertEqual(code, 1, buggy)
            self.assertEqual(buggy['stats']['unexpected'], 1)
            payload = json.dumps(buggy)
            self.assertIn('observed-outcomes', payload)
            self.assertNotIn('SyntaxError', payload)
            state['fixed'] = True
            code, fixed = await self.execute(source)
            self.assertEqual(code, 0, fixed)
            self.assertEqual(fixed['stats']['expected'], 1)

    async def test_response_oracle_export_uses_registered_session_and_delay(self):
        with condition_site() as (origin, policy, state, _):
            source = export_playwright_test(response_plan(origin + '/app'), policy=policy,
                options=BrowserOptions(test_context_id='staff', session_headers={'Authorization': 'secret'}, response_delay_ms=30))
            code, buggy = await self.execute(source, extra_env={'QA_AUTHORIZATION': 'Bearer synthetic-staff'})
            self.assertEqual(code, 1, buggy)
            state['buggy'] = False
            code, fixed = await self.execute(source, extra_env={'QA_AUTHORIZATION': 'Bearer synthetic-staff'})
            self.assertEqual(code, 0, fixed)

    async def test_http_entry_override_cannot_send_a_registered_session(self):
        with site() as (origin, hits, _):
            secure = origin.replace('http:', 'https:')
            scenario = decode_plan(plan(secure + '/app/mixed')['plan'], timeout_ms=1000)
            source = export_playwright_test(scenario, policy=local_policy(secure),
                options=BrowserOptions(test_context_id='staff', session_headers={'Authorization': 'synthetic'}))
            code, report = await self.execute(source, extra_env={
                'QA_ENTRY_URL': origin + '/app/mixed', 'QA_AUTHORIZATION': 'Bearer synthetic-no-plaintext'})
            self.assertEqual(code, 1, report)
            self.assertIn('CREDENTIALS_REQUIRE_HTTPS', json.dumps(report))
            self.assertNotIn('synthetic-no-plaintext', json.dumps(report))
            self.assertEqual(hits, [])

    async def test_authenticated_redirect_never_reaches_plaintext_destination(self):
        with site() as (plain, plain_hits, _), site(tls=True) as (secure, secure_hits, state):
            state['redirect_location'] = plain + '/app/bug'
            scenario = decode_plan(plan(secure + '/app/redirect')['plan'], timeout_ms=1000)
            source = export_playwright_test(scenario, policy=local_policy(secure),
                options=BrowserOptions(test_context_id='staff', session_headers={'Cookie': 'session=synthetic'}))
            code, report = await self.execute(source, extra_env={'QA_COOKIE': 'session=synthetic'})
            self.assertEqual(code, 1, report)
            self.assertEqual([hit[1] for hit in secure_hits], ['/app/redirect'])
            self.assertEqual(plain_hits, [])

    async def test_visual_and_boolean_predicates_compile_and_run(self):
        with site() as (origin, _, _):
            checks = ((Check('overlap', '#a', 'false', '#b'), Check('overlap', '#a', 'true', '#b')),
                      (Check('clipped', '#clip', 'true'), Check('clipped', '#clip', 'false')),
                      (Check('enabled', '#a', 'true'), Check('enabled', '#a', 'false')),
                      (Check('count', '#a', '1'), Check('visible', '#absent', 'true')))
            for expected, symptom in checks:
                scenario = BrowserPlan((Step('navigate', origin + '/app/visual-fixed'),),
                    ready=Check('visible', '#a', 'true'), expected=expected, symptom=symptom)
                code, report = await self.execute(export_playwright_test(scenario, policy=local_policy(origin)))
                self.assertEqual(code, 0, report)
