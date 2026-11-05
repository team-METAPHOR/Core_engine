from dataclasses import replace
from io import BytesIO
import json
import tempfile
import threading
import unittest
from unittest.mock import patch

from PIL import Image

from core_engine import (BrowserOptions, BrowserPlan, BrowserRunner, BudgetLimits, Check,
                         JobStore, Recorder, RequestBudget, Step)
from core_engine.transport import GuardedTransport
from fixture_site import site
from test_transport import local_policy


async def reset(_):
    return True


class ObservationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sensitive_dom_input_and_console_are_masked(self):
        with tempfile.TemporaryDirectory() as root, site() as (origin, _, _):
            store = JobStore(root)
            job = store.create()
            recorder = Recorder(store, job, BrowserOptions())
            runner = BrowserRunner(local_policy(origin), recorder=recorder)
            snapshot = await runner.inspect(origin + '/app/privacy', reset=reset, is_authorized=lambda: True)
            serialized = json.dumps(snapshot)
            for secret in ('customer-canary', 'password-canary', 'input-canary', 'private@example.com', 'console-canary'):
                self.assertNotIn(secret, serialized)
            image = store.read_image(job, snapshot['image'])
            with Image.open(BytesIO(image.data)) as pixels:
                self.assertIn((255, 0, 255), pixels.getdata())
            self.assertTrue(snapshot['errors'])
            self.assertTrue(snapshot['network'])

    async def test_visual_overlap_and_fixed_layout_are_deterministic(self):
        with site() as (origin, _, _):
            for path, expected in (('/app/visual', 'reproduced'), ('/app/visual-fixed', 'not_reproduced')):
                plan = BrowserPlan((Step('navigate', origin + path),), Check('text', '#ready', 'done'),
                                   Check('overlap', '#a', 'true', '#b'), expected=Check('overlap', '#a', 'false', '#b'))
                result = await BrowserRunner(local_policy(origin)).run(plan, reset=reset, is_authorized=lambda: True)
                self.assertEqual(result.verdict.value, expected, result)

    async def test_visibility_count_enabled_and_clipping(self):
        with site() as (origin, _, _):
            pairs = [
                (Check('visible', '#missing', 'false'), Check('visible', '#missing', 'true')),
                (Check('count', 'button', '2'), Check('count', 'button', '0')),
                (Check('enabled', '#a', 'true'), Check('enabled', '#a', 'false')),
                (Check('clipped', '#clip', 'true'), Check('clipped', '#clip', 'false')),
            ]
            for symptom, expected in pairs:
                plan = BrowserPlan((Step('navigate', origin + '/app/visual'),), Check('text', '#ready', 'done'),
                                   symptom, expected=expected)
                result = await BrowserRunner(local_policy(origin)).run(plan, reset=reset, is_authorized=lambda: True)
                self.assertEqual(result.verdict.value, 'reproduced', result)

    async def test_missing_and_ambiguous_check_targets_are_inconclusive(self):
        with site() as (origin, _, _):
            for target in ('#missing', 'button'):
                plan = BrowserPlan((Step('navigate', origin + '/app/visual'),), Check('text', '#ready', 'done'),
                                   Check('text', target, 'wrong'), expected=Check('text', target, 'normal'))
                result = await BrowserRunner(local_policy(origin)).run(plan, reset=reset, is_authorized=lambda: True)
                self.assertEqual(result.verdict.value, 'inconclusive', result)

    def test_malformed_checks_and_options_rejected(self):
        for fields in (('execute', '#a', 'code'), ('count', '#a', '-1'), ('visible', '#a', 'yes'),
                       ('overlap', '#a', 'true'), ('text', '#a', 'value', '#b')):
            with self.assertRaises(ValueError):
                Check(*fields)
        with self.assertRaises(ValueError):
            BrowserOptions(width=True)
        with self.assertRaises(ValueError):
            BrowserOptions(step_timeout_ms=6000)


class SessionCredentialTests(unittest.TestCase):
    def test_trusted_headers_forward_only_through_guarded_transport(self):
        with site(tls=True) as (origin, _, _):
            from http.client import HTTPConnection
            original = HTTPConnection.request
            sent = []
            def capture(connection, *args, **kwargs):
                sent.append(kwargs.get('headers', {}).copy())
                return original(connection, *args, **kwargs)
            transport = GuardedTransport(local_policy(origin), RequestBudget(BudgetLimits()), lambda: True,
                                         threading.Event(), session_headers={'Cookie': 'session=synthetic', 'Authorization': 'Bearer synthetic'})
            with patch.object(HTTPConnection, 'request', capture):
                transport.fetch(origin + '/app/bug')
            transport.close()
            self.assertEqual(sent[0]['Cookie'], 'session=synthetic')
            self.assertEqual(sent[0]['Authorization'], 'Bearer synthetic')
            options = BrowserOptions(test_context_id='synthetic-user', session_headers={'Authorization': 'Bearer private-key'})
            self.assertNotIn('private-key', repr(options))
            self.assertNotIn('private-key', json.dumps(options.public()))
            for bad in ({'Host': 'attacker'}, {'Cookie': 'x\r\nHost: attacker'}):
                with self.assertRaises(ValueError):
                    GuardedTransport(local_policy(origin), RequestBudget(BudgetLimits()), lambda: True,
                                     threading.Event(), session_headers=bad)


if __name__ == '__main__':
    unittest.main()
