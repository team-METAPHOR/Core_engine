from dataclasses import asdict, replace
from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image
from core_engine import (BrowserOptions, BrowserPlan, BrowserRunner, Check, JobStore,
                         LoginOptions, PolicyError, QARequest, QAService, Recorder, Step, export_playwright_test)
from core_engine.qa_schema import encode_plan
from core_engine.test_export import required_test_environment
from login_fixture import LOGIN_ID, PASSWORD, login_site
from qa_fixtures import ScriptedModel, analysis, intake
from test_transport import local_policy
import test_test_export


def options(**kwargs):
    login = LoginOptions('/app/login', LOGIN_ID, PASSWORD, submit_path='/app/auth', success_path='/app/home', timeout_ms=1000)
    return BrowserOptions(test_context_id='test-account-v1', login=login, **kwargs)


def scenario(origin, path='/app/bug'):
    return BrowserPlan((Step('navigate', origin + path),), Check('text', '#ready', 'done'),
                       Check('text', '#result', 'broken'), expected=Check('text', '#result', 'normal'))


async def reset(_):
    return True


class LoginContractTests(unittest.TestCase):
    def test_credentials_are_private_and_success_signal_is_required(self):
        config = options()
        for payload in (repr(config), repr(config.login), json.dumps(config.public()), json.dumps(config.login.configuration())):
            self.assertNotIn(LOGIN_ID, payload)
            self.assertNotIn(PASSWORD, payload)
        self.assertEqual(config.public()['session_mode'], 'form_login')
        with self.assertRaises(ValueError):
            LoginOptions('/app/login', LOGIN_ID, PASSWORD)
        for kwargs in ({'test_context_id': 'anonymous'}, {'session_headers': {'Cookie': 'private'}}):
            with self.assertRaises(ValueError):
                replace(config, **kwargs)
        for path in ('https://outside.test/login', '//outside.test/login', '/app/../outside', '/app/login?password=value'):
            with self.assertRaises(ValueError):
                replace(config.login, login_path=path)


class LoginBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_anonymous_probe_and_delayed_login_form(self):
        for allowed in (False, True):
            with self.subTest(allowed=allowed), login_site() as (origin, state):
                state.update(probe=True, external_sdk=True)
                config = options()
                if allowed:
                    config = replace(config, login=replace(config.login, unauthenticated_paths=('/app/status',)))
                result = await BrowserRunner(local_policy(origin), options=config).run(
                    scenario(origin), reset=reset, is_authorized=lambda: True)
                self.assertEqual(result.verdict.value, 'reproduced' if allowed else 'inconclusive', result)
                self.assertEqual(state['logins'], 2 if allowed else 0)
                self.assertFalse(any('widget.js' in path for _, path in state['hits']))

    async def test_discovery_and_both_attempts_use_fresh_login_without_recording_it(self):
        with login_site() as (origin, state), tempfile.TemporaryDirectory() as directory:
            config = options(record_video=True)
            store = JobStore(directory); job = store.create()
            captured = []
            class Recording(Recorder):
                async def masked_image(self, page):
                    captured.append(page.url)
                    return await super().masked_image(page)
            recorder = Recording(store, job, config)
            runner = BrowserRunner(local_policy(origin), options=config, recorder=recorder)
            snapshot = await runner.inspect(origin + '/app/bug', reset=reset, is_authorized=lambda: True)
            result = await runner.run(scenario(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.value, 'reproduced', result)
            self.assertEqual(state['logins'], 3)
            self.assertEqual(len(set(state['sessions'])), 3)
            self.assertTrue(captured)
            self.assertFalse(any('/app/login' in url or '/app/auth' in url for url in captured), captured)
            self.assertTrue(all(v['status'] == 'ready' for v in recorder.videos), recorder.videos)
            self.assertTrue(all(v['capture_error'] is None for v in recorder.videos), recorder.videos)
            self.assertFalse(snapshot['errors'])
            self.assertFalse(any('/app/login' in r['url'] or '/app/auth' in r['url'] for r in snapshot['network']))
            self.assertNotIn(PASSWORD, json.dumps(asdict(result)))

    async def test_bad_account_returns_login_failed_without_credential_evidence(self):
        with login_site() as (origin, state), tempfile.TemporaryDirectory() as directory:
            config = replace(options(record_video=True), login=replace(options().login, password='wrong-password-canary', timeout_ms=250))
            store = JobStore(directory); job = store.create(); recorder = Recorder(store, job, config)
            result = await BrowserRunner(local_policy(origin), options=config, recorder=recorder).run(
                scenario(origin), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.value, 'inconclusive')
            self.assertEqual([r.blocker for r in result.attempts], ['LOGIN_FAILED'] * 2)
            self.assertEqual([r.stage for r in result.evidence], ['login'] * 2)
            self.assertEqual(recorder.snapshots, [])
            self.assertEqual(recorder.videos, [])
            self.assertEqual(state['logins'], 0)
            self.assertFalse(any(path == '/app/bug' for _, path in state['hits']))

    async def test_login_scope_does_not_enable_qa_post_requests(self):
        with login_site() as (origin, state):
            with self.assertRaises(PolicyError):
                BrowserRunner(local_policy(origin), options=replace(options(), login=replace(options().login, login_path='/outside')))
            self.assertEqual(state['hits'], [])
            plan = replace(scenario(origin), steps=(*scenario(origin).steps, Step('click', '#write')))
            result = await BrowserRunner(local_policy(origin), options=options()).run(plan, reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.value, 'inconclusive')
            self.assertFalse(any(method == 'POST' and path != '/app/auth' for method, path in state['hits']))

    async def test_reflected_password_is_masked_and_not_sent_to_models_or_results(self):
        with login_site() as (origin, state), tempfile.TemporaryDirectory() as directory:
            config=options()
            store=JobStore(directory)
            proposal={'status':'ready','questions':[],'rationale':'정상과 결함 문구 비교', 'plan':encode_plan(scenario(origin, '/app/echo'))}
            model=ScriptedModel([intake(), proposal, analysis(), analysis()])
            service=QAService(model, local_policy(origin), store, options=config)
            request=QARequest('login-report', 'demo', origin+'/app/echo', description='화면 결과가 broken으로 보입니다.', expected_result='정상 결과는 normal입니다.')
            result=await service.run(request, reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.status, 'completed', result.error_code)
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
            self.assertEqual(result.report['test_export']['status'], 'ready')
            for call in model.calls:
                serialized=json.dumps(call['context'])
                self.assertNotIn(PASSWORD, serialized)
                self.assertNotIn(LOGIN_ID, serialized)
            snapshot=result.report['observations'][0]
            with Image.open(BytesIO(store.read_image(result.job_id, snapshot['image']).data)) as image:
                self.assertIn((255,0,255), image.getdata())
            for path in Path(directory).rglob('*'):
                if path.is_file() and path.suffix in {'.json','.md','.ts'}:
                    self.assertNotIn(PASSWORD, path.read_text())
                    self.assertNotIn(LOGIN_ID, path.read_text())
            state['fixed']=True
            fixed=await service.retest(result.job_id, reset=reset, is_authorized=lambda: True)
            self.assertEqual(fixed.report['regression'], 'resolved_in_test', fixed.error_code)
            changed=QAService(model,local_policy(origin),store,options=replace(config,
                login=replace(config.login,success_selector='#account')))
            mismatch=await changed.retest(result.job_id,reset=reset,is_authorized=lambda:True)
            self.assertEqual(mismatch.error_code,'BASELINE_ENVIRONMENT_MISMATCH')


@unittest.skipUnless((test_test_export.JS/'node_modules/@playwright/test/cli.js').exists(), 'install the pinned JS dependencies')
class LoginExportTests(unittest.IsolatedAsyncioTestCase):
    execute=test_test_export.ExecutedExportTests.execute

    async def test_export_uses_env_credentials_and_fails_bug_passes_fix(self):
        with login_site() as (origin,state):
            source=export_playwright_test(scenario(origin),policy=local_policy(origin),options=options())
            self.assertNotIn(PASSWORD,source)
            self.assertNotIn(LOGIN_ID,source)
            self.assertIn('QA_LOGIN_PASSWORD',required_test_environment(scenario(origin),options()))
            env={'QA_LOGIN_ID':LOGIN_ID,'QA_LOGIN_PASSWORD':PASSWORD}
            code,report=await self.execute(source,extra_env=env)
            self.assertEqual(code,1,report)
            self.assertNotIn(PASSWORD,json.dumps(report))
            state['fixed']=True
            code,report=await self.execute(source,extra_env=env)
            self.assertEqual(code,0,report)
            code,report=await self.execute(source,extra_env={**env,'QA_LOGIN_PASSWORD':'wrong-password-canary'})
            self.assertEqual(code,1,report)
            self.assertIn('LOGIN_FAILED',json.dumps(report))
            self.assertNotIn('wrong-password-canary',json.dumps(report))
