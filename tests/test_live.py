import asyncio
from io import BytesIO
import json
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from PIL import Image
from core_engine import (BrowserOptions, BrowserPlan, BrowserRunner, Check, ConditionsService,
                         JobStore, LiveOptions, LiveStream, QARequest, QAService, Recorder, Step, TestCondition)
from fixture_site import site
from login_fixture import LOGIN_ID, PASSWORD, login_site
from qa_fixtures import ScriptedModel, analysis, intake, plan, screenshot
from test_login import options as login_options, scenario as login_scenario
from test_transport import local_policy


async def reset(*_):
    return True


def bound_stream(**options):
    stream = LiveStream(LiveOptions(**options))
    stream._bind('a' * 32, 'demo', 'report-1')
    return stream


class LiveBufferTests(unittest.TestCase):
    def test_bounded_history_latest_frame_cursor_and_wire_contract(self):
        live = bound_stream(event_capacity=8)
        image = screenshot()
        for index in range(100):
            live._status('running', {'revision': index, 'secret': 'never-relay-this'})
            with patch('core_engine.live.time.monotonic', return_value=index + 100):
                live._frame(image)
        messages = live.read()
        self.assertEqual(len(messages), 9)
        self.assertEqual(len([m for m in messages if m.kind == 'frame']), 1)
        self.assertEqual([m.sequence for m in messages], sorted(m.sequence for m in messages))
        self.assertEqual(live.read(messages[-1].sequence), ())
        self.assertNotIn('never-relay-this', str([m.metadata() for m in messages]))
        frame = messages[-1]
        wire = frame.to_wire()
        length = int.from_bytes(wire[:4], 'big')
        self.assertEqual(json.loads(wire[4:4 + length]), frame.metadata())
        self.assertEqual(wire[4 + length:], frame.image)
        self.assertEqual(len(frame.image), frame.metadata()['data']['bytes'])
        self.assertEqual(Image.open(BytesIO(frame.image)).format, 'JPEG')
        self.assertEqual(json.loads(messages[0].to_wire()), messages[0].metadata())
        # A consumer cannot modify stored metadata, and repr never dumps image bytes.
        frame.metadata()['data']['phase'] = 'tampered'
        self.assertNotIn('tampered', frame.to_wire().decode('latin1'))
        self.assertNotIn('image=', repr(frame))

    def test_throttle_new_attempt_and_terminal_cancellation(self):
        live = bound_stream()
        image = screenshot()
        with patch('core_engine.live.time.monotonic', return_value=10):
            live._frame(image)
            first = live.read()[-1].sequence
            live._frame(image)
            self.assertEqual(live.read()[-1].sequence, first)
            live._progress(attempt=2, revision=0, phase='attempt_started')
            self.assertFalse(any(m.image for m in live.read()))
            live._frame(image)
        self.assertEqual(live.read()[-1].metadata()['data']['attempt'], 2)
        live._end('cancelled', error_code='CANCELLED')
        self.assertTrue(live.closed)
        self.assertFalse(any(m.image for m in live.read()))
        before = live.read()
        live._frame(image)
        live._status('running')
        live._end('completed')
        self.assertEqual(live.read(), before)

    def test_failed_encoding_is_bounded_and_does_not_raise(self):
        live = bound_stream()
        image = screenshot()
        for index in range(10):
            with patch('core_engine.live.time.monotonic', return_value=index + 100), \
                 patch('core_engine.live.Image.open', side_effect=ValueError('secret-provider-body')):
                live._frame(image)
        self.assertEqual(len(live.read()), 1)
        self.assertEqual(live.read()[0].metadata()['data']['error_code'], 'LIVE_FRAME_ENCODING_FAILED')
        self.assertNotIn('secret-provider-body', live.read()[0].to_wire())

    def test_large_frame_is_dropped_and_authorization_end_clears_cached_frame(self):
        live = bound_stream(max_frame_bytes=16 * 1024)
        from core_engine import Screenshot
        noise = Image.effect_noise((960, 960), 100).convert('RGB')
        buffer = BytesIO(); noise.save(buffer, format='PNG')
        live._frame(Screenshot(buffer.getvalue()))
        self.assertEqual(live.read()[-1].metadata()['data']['error_code'], 'LIVE_FRAME_TOO_LARGE')
        self.assertFalse(any(m.image for m in live.read()))
        with patch('core_engine.live.time.monotonic', return_value=time.monotonic() + 2):
            live._frame(screenshot())
        live._end('inconclusive', error_code='AUTHORIZATION_REVOKED')
        self.assertFalse(any(m.image for m in live.read()))

    def test_separate_worker_reader_thread_and_validation(self):
        live = bound_stream()
        errors = []
        def read():
            try:
                for _ in range(200):
                    messages = live.read()
                    self.assertEqual(len({m.sequence for m in messages}), len(messages))
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=read); thread.start()
        for _ in range(200):
            live._status('running')
        thread.join()
        self.assertEqual(errors, [])
        for kwargs in ({'fps': 0}, {'fps': True}, {'max_width': 9000}, {'event_capacity': 1000}):
            with self.assertRaises(ValueError):
                LiveOptions(**kwargs)
        for cursor in (-1, True, '1'):
            with self.assertRaises(ValueError):
                live.read(cursor)
        with self.assertRaises(ValueError):
            live._bind('b' * 32, 'other-program', 'other-report')


class LiveBrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = JobStore(temporary.name)

    async def collect(self, task, live):
        messages, cursor, seen_before_finish = [], 0, False
        while not task.done():
            new = live.read(cursor)
            if new:
                cursor = new[-1].sequence
                messages.extend(new)
                seen_before_finish |= any(m.image for m in new)
            await asyncio.sleep(.02)
        messages.extend(live.read(cursor))
        return await task, messages, seen_before_finish

    def recorder(self, options):
        job = self.store.create()
        live = LiveStream()
        live._bind(job, 'demo', 'report-1')
        return Recorder(self.store, job, options, live=live), live

    async def test_real_frames_arrive_before_completion_mask_inputs_and_run_without_video(self):
        with site() as (origin, _, _):
            options = BrowserOptions(step_timeout_ms=1000)
            recorder, live = self.recorder(options)
            scenario = BrowserPlan((Step('navigate', origin + '/app/privacy'), Step('click', '#missing')),
                ready=Check('visible', '#search', 'true'), expected=Check('count', '#search', '1'),
                symptom=Check('count', '#search', '2'), step_timeout_ms=600)
            task = asyncio.create_task(BrowserRunner(local_policy(origin), options=options, recorder=recorder).run(
                scenario, reset=reset, is_authorized=lambda: True))
            result, messages, early = await self.collect(task, live)
            self.assertTrue(early)
            self.assertEqual(result.verdict.value, 'inconclusive')
            frames = [m for m in messages if m.image]
            self.assertGreaterEqual(len(frames), 2)
            self.assertEqual({m.metadata()['data']['attempt'] for m in frames}, {1, 2})
            for frame in frames:
                image = Image.open(BytesIO(frame.image)).convert('RGB')
                self.assertLessEqual(image.width, 960)
                masked = sum(r > 180 and g < 80 and b > 180 for r, g, b in image.get_flattened_data())
                self.assertGreater(masked, 300)
            metadata = json.dumps([m.metadata() for m in messages])
            for value in ('customer-canary', 'password-canary', 'input-canary', '#missing', origin):
                self.assertNotIn(value, metadata)
            self.assertEqual(recorder.videos, [])
            self.assertFalse(list((self.store.root / recorder.job_id).glob('*.jpeg')))

    async def test_login_pages_excluded_and_missing_encoder_does_not_stop_preview(self):
        with login_site() as (origin, state):
            options = login_options(record_video=True)
            recorder, live = self.recorder(options)
            urls = []
            original = recorder.masked_image
            async def capture(page):
                urls.append(page.url)
                return await original(page)
            recorder.masked_image = capture
            with patch('core_engine.video.find_encoder', return_value=None):
                result = await BrowserRunner(local_policy(origin), options=options, recorder=recorder).run(
                    login_scenario(origin, '/app/echo'), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.value, 'reproduced')
            self.assertEqual(state['logins'], 2)
            self.assertFalse(any('/app/login' in u or '/app/auth' in u for u in urls))
            self.assertTrue(any(m.image for m in live.read()))
            self.assertEqual([v['status'] for v in recorder.videos], ['unavailable', 'unavailable'])
            self.assertNotIn(PASSWORD, str([m.metadata() for m in live.read()]))
            self.assertNotIn(LOGIN_ID, str([m.metadata() for m in live.read()]))

    async def test_qa_lifecycle_recording_and_fixed_plan_retest(self):
        with site() as (origin, _, state):
            model = ScriptedModel([intake(), plan(origin + '/app/mixed'), analysis(), analysis()])
            service = QAService(model, local_policy(origin), self.store, options=BrowserOptions(record_video=True))
            request = QARequest('report-1', 'demo', origin + '/app/mixed', screenshot(),
                                'cat 검색 후 missing 검색 시 이전 목록이 남음', '빈 목록 표시')
            live = LiveStream()
            task = asyncio.create_task(service.run(request, reset=reset, is_authorized=lambda: True, live=live))
            result, messages, early = await self.collect(task, live)
            self.assertTrue(early)
            self.assertEqual(result.status, 'completed', result.error_code)
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
            self.assertEqual([v['status'] for v in result.report['videos']], ['ready'] * 3)
            self.assertTrue(live.closed)
            self.assertEqual(messages[-1].kind, 'end')
            self.assertEqual(messages[-1].metadata()['data']['status'], result.status)
            self.assertTrue(self.store.read(result.job_id, 'report.md'))
            self.assertEqual({m.job_id for m in messages}, {result.job_id})
            self.assertEqual({m.metadata()['data']['attempt'] for m in messages if m.kind == 'progress'}, {0, 1, 2})
            self.assertTrue(any(m.metadata()['data'].get('action') == 'click' for m in messages))
            # Independent stream for the next run, preserving the exact baseline plan.
            state['fixed'] = True
            retest_live = LiveStream()
            retest = await service.retest(result.job_id, reset=reset, is_authorized=lambda: True, live=retest_live)
            self.assertEqual(retest.report['regression'], 'resolved_in_test')
            self.assertTrue(retest_live.closed)
            self.assertNotEqual(live.job_id, retest_live.job_id)
            with self.assertRaises(ValueError):
                await service.run(request, reset=reset, is_authorized=lambda: True, live=live)

    async def test_needs_info_resume_and_failed_model_close_stream_without_frames(self):
        with site() as (origin, _, _):
            service = QAService(ScriptedModel([intake('needs_info'), intake('needs_info')]),
                                local_policy(origin), self.store)
            request = QARequest('report-1', 'demo', origin + '/app/mixed', screenshot(), '화면 이상')
            live = LiveStream()
            result = await service.run(request, reset=reset, is_authorized=lambda: True, live=live)
            self.assertEqual(result.status, 'needs_info')
            self.assertTrue(live.closed)
            self.assertFalse(any(m.image for m in live.read()))
            resumed = LiveStream()
            await service.resume(result.job_id, '검색 후 빈 목록이 보여야 합니다.', reset=reset,
                                 is_authorized=lambda: True, live=resumed)
            self.assertTrue(resumed.closed)
            from core_engine import ModelError
            service.model = ScriptedModel([ModelError('MODEL_UNAVAILABLE')])
            failed = LiveStream()
            result = await service.run(request, reset=reset, is_authorized=lambda: True, live=failed)
            self.assertEqual(result.status, 'failed')
            self.assertEqual(failed.read()[-1].metadata()['data']['error_code'], 'MODEL_UNAVAILABLE')
            self.assertTrue(failed.closed)

    async def test_task_cancel_and_authorization_revocation_clear_cached_frames(self):
        with site() as (origin, _, _):
            for revoke in (False, True):
                gate = threading.Event()
                allowed = [True]
                def wait_for_release(_):
                    gate.wait(2)
                    return plan(origin + '/app/mixed')
                service = QAService(ScriptedModel([intake(), wait_for_release, analysis()]),
                                    local_policy(origin), self.store)
                live = LiveStream()
                request = QARequest('report-1', 'demo', origin + '/app/mixed', screenshot(),
                                    'cat 검색 후 missing 검색 시 이전 목록이 남음', '빈 목록 표시')
                task = asyncio.create_task(service.run(request, reset=reset, is_authorized=lambda: allowed[0], live=live))
                try:
                    async with asyncio.timeout(5):
                        while not any(m.image for m in live.read()):
                            await asyncio.sleep(.02)
                    if revoke:
                        allowed[0] = False
                        result = await task
                        self.assertEqual(result.error_code, 'AUTHORIZATION_REVOKED')
                    else:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    self.assertTrue(live.closed)
                    self.assertFalse(any(m.image for m in live.read()))
                finally:
                    gate.set()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    async def test_condition_jobs_use_independent_streams(self):
        with site() as (origin, _, _):
            model = ScriptedModel([intake(), plan(origin + '/app/mixed'), analysis()] * 2)
            conditions = tuple(TestCondition(key, key, BrowserOptions()) for key in ('one', 'two'))
            service = ConditionsService(model, local_policy(origin), self.store, conditions)
            request = QARequest('report-1', 'demo', origin + '/app/mixed', screenshot(),
                                'cat 검색 후 missing 검색 시 이전 목록이 남음', '빈 목록 표시')
            streams = {}
            def factory(condition_id):
                streams[condition_id] = LiveStream()
                return streams[condition_id]
            result = await service.run(request, reset=reset, is_authorized=lambda: True, select=False, live_factory=factory)
            self.assertEqual(result.status, 'completed')
            self.assertEqual(set(streams), {'one', 'two'})
            for row in result.report['conditions']:
                stream = streams[row['condition_id']]
                self.assertEqual(row['job_id'], stream.job_id)
                self.assertTrue(stream.closed)
                self.assertTrue(any(m.image for m in stream.read()))

    async def test_preflight_denial_closes_empty_stream_and_capture_rechecks_permission(self):
        from core_engine import PolicyError
        from types import SimpleNamespace
        from core_engine.session import GuardedPage
        policy = local_policy('http://127.0.0.1:12345')
        service = QAService(ScriptedModel([]), policy, self.store)
        live = LiveStream()
        with self.assertRaises(PolicyError) as denied:
            await service.run(QARequest('report-1', 'demo', 'http://127.0.0.1:12345/app', description='broken'),
                              reset=reset, is_authorized=lambda: False, live=live)
        self.assertEqual(denied.exception.code, 'AUTHORIZATION_REVOKED')
        self.assertTrue(live.closed)
        self.assertIsNone(live.job_id)
        self.assertEqual(live.read(), ())
        allowed = [True]
        def guard():
            if not allowed[0]:
                raise PolicyError('AUTHORIZATION_REVOKED')
        recorder, capture_live = self.recorder(BrowserOptions())
        async def masked(_):
            allowed[0] = False  # Permission revoked during the asynchronous screenshot operation.
            return screenshot(), []
        recorder.masked_image = masked
        session = GuardedPage(None, SimpleNamespace(check=guard, policy=policy), BrowserOptions(), recorder)
        session.page = SimpleNamespace(url='http://127.0.0.1:12345/app')
        with self.assertRaises(PolicyError):
            await session.capture_video_frame()
        allowed[0] = True
        with self.assertRaises(PolicyError):
            await session.capture(1, 0, 'after')
        self.assertFalse(any(m.image for m in capture_live.read()))


if __name__ == '__main__':
    unittest.main()
