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
    async def execute(self, source, *, extra_env=None):
        with tempfile.TemporaryDirectory(prefix='export-', dir=JS) as temporary:
            root = Path(temporary)
            (root / 'scenario.spec.ts').write_text(source)
            env = {key: value for key, value in os.environ.items() if not key.startswith('QA_')}
            env.update(QA_RESET_MODULE=str((JS / 'reset.mjs').resolve()), **(extra_env or {}))
            process = await asyncio.create_subprocess_exec('node', str(JS / 'node_modules/@playwright/test/cli.js'),
                'test', str(root / 'scenario.spec.ts'), '--reporter=json', '--workers=1', '--output=' + str(root / 'results'),
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
