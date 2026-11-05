from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import tempfile
import unittest

from core_engine import BrowserOptions, BrowserPlan, BrowserRunner, Check, JobStore, Recorder, Step
from core_engine.policy import PolicyError
from test_transport import local_policy


@contextmanager
def history_site():
    state = {"fixed": False, "hits": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            state["hits"].append(self.path)
            payload = '''<!doctype html><meta charset="utf-8">
<div id="ready">loaded</div><button id="select" aria-pressed="false">Select</button>
<button id="next">Next</button><button id="same">Same URL entry</button>
<button id="escape">Out of scope history</button><div id="missing">no state</div>
<div id="busy" aria-busy="false">Status</div><button id="noop">No operation</button>
<button id="complete">Complete operation</button>
<script>
const fixed = FIXED;
const selected = document.querySelector('#select');
if (fixed && sessionStorage.getItem('selected')) selected.setAttribute('aria-pressed','true');
selected.onclick = () => { selected.setAttribute('aria-pressed','true'); sessionStorage.setItem('selected','yes'); };
document.querySelector('#next').onclick = () => {
  history.pushState({}, '', '/app/details'); document.querySelector('#ready').textContent = 'details';
};
document.querySelector('#same').onclick = () => { history.pushState({}, '', location.href); };
document.querySelector('#complete').onclick = () => { document.querySelector('#busy').setAttribute('aria-busy','false'); };
document.querySelector('#escape').onclick = () => { history.pushState({}, '', '/outside'); history.pushState({}, '', '/app/details'); };
addEventListener('popstate', () => {
  selected.setAttribute('aria-pressed', fixed ? 'true' : 'false');
  document.querySelector('#ready').textContent = 'returned';
});
</script>'''.replace('FIXED', 'true' if state['fixed'] else 'false').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def history_plan(origin, action='reload', button=None):
    return BrowserPlan((Step('navigate', origin + '/app/history'),
                        Step('click', button or ('#select' if action == 'reload' else '#next')), Step(action, '')),
        Check('text', '#ready', 'loaded' if action == 'reload' else 'returned'),
        Check('attribute', '#select', 'false', 'aria-pressed'),
        expected=Check('attribute', '#select', 'true', 'aria-pressed'), step_timeout_ms=500)


async def reset(_):
    return True


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_stale_aria_readiness_needs_fresh_completion_evidence(self):
        with history_site() as (origin, _):
            for button, verdict in [('#noop', 'inconclusive'), ('#complete', 'reproduced')]:
                scenario = replace(history_plan(origin),
                    steps=(Step('navigate', origin + '/app/history'), Step('click', button)),
                    ready=Check('attribute', '#busy', 'false', 'aria-busy'))
                result = await BrowserRunner(local_policy(origin)).run(scenario, reset=reset, is_authorized=lambda: True)
                self.assertEqual(result.verdict.value, verdict, result)
                if button == '#noop':
                    self.assertEqual([e.blocker for e in result.evidence], ['STALE_COMPLETION_SIGNAL'] * 2)

    async def test_reload_and_spa_back_distinguish_bug_and_fixed_twice(self):
        with history_site() as (origin, state):
            for action, button in [('reload', None), ('back', None), ('back', '#same')]:
                scenario = history_plan(origin, action, button)
                for fixed in (False, True):
                    state['fixed'] = fixed
                    result = await BrowserRunner(local_policy(origin)).run(scenario, reset=reset, is_authorized=lambda: True)
                    self.assertEqual(result.verdict.value, 'not_reproduced' if fixed else 'reproduced', result)
                    self.assertEqual([e.completed_steps for e in result.evidence], [3, 3])

    async def test_back_without_history_cannot_be_success(self):
        with history_site() as (origin, _):
            scenario = replace(history_plan(origin, 'back'), steps=(Step('navigate', origin + '/app/history'), Step('back', '')))
            for name in ('chromium', 'firefox', 'webkit'):
                result = await BrowserRunner(local_policy(origin), options=BrowserOptions(browser_name=name)).run(
                    scenario, reset=reset, is_authorized=lambda: True)
                self.assertEqual(result.verdict.value, 'inconclusive')
                self.assertEqual([e.blocker for e in result.evidence], ['HISTORY_UNAVAILABLE'] * 2, name)

    async def test_firefox_webkit_support_same_document_history_and_reload(self):
        with history_site() as (origin, state):
            state['fixed'] = True
            for name in ('firefox', 'webkit'):
                for action in ('reload', 'back'):
                    result = await BrowserRunner(local_policy(origin), options=BrowserOptions(browser_name=name)).run(
                        history_plan(origin, action), reset=reset, is_authorized=lambda: True)
                    self.assertEqual(result.verdict.value, 'not_reproduced', result)

    async def test_history_without_network_is_still_scope_checked_before_capture(self):
        with history_site() as (origin, state), tempfile.TemporaryDirectory() as root:
            store = JobStore(root)
            recorder = Recorder(store, store.create(), BrowserOptions())
            result = await BrowserRunner(local_policy(origin), recorder=recorder).run(
                history_plan(origin, 'back', '#escape'), reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.value, 'inconclusive')
            self.assertEqual([e.blocker for e in result.evidence], ['PATH_OUT_OF_SCOPE'] * 2)
            self.assertNotIn('/outside', state['hits'])
            self.assertFalse(any('/outside' in s['url'] for s in recorder.snapshots))

    async def test_url_path_and_full_document_back(self):
        with history_site() as (origin, _):
            scenario = BrowserPlan((Step('navigate', origin + '/app/history'),
                Step('navigate', origin + '/app/other'), Step('back', '')),
                Check('text', '#ready', 'loaded'), Check('url_path', '', '/app/other'),
                expected=Check('url_path', '', '/app/history'), step_timeout_ms=500)
            result = await BrowserRunner(local_policy(origin)).run(scenario, reset=reset, is_authorized=lambda: True)
            self.assertEqual(result.verdict.value, 'not_reproduced', result)

    async def test_missing_state_is_inconclusive_and_snapshot_exposes_only_states(self):
        with history_site() as (origin, _), tempfile.TemporaryDirectory() as root:
            store = JobStore(root)
            recorder = Recorder(store, store.create(), BrowserOptions())
            scenario = replace(history_plan(origin),
                expected=Check('attribute', '#missing', 'true', 'aria-pressed'),
                symptom=Check('attribute', '#missing', 'false', 'aria-pressed'))
            result = await BrowserRunner(local_policy(origin), recorder=recorder).run(scenario, reset=reset, is_authorized=lambda: True)
            self.assertEqual([e.blocker for e in result.evidence], ['CHECK_ATTRIBUTE_UNAVAILABLE'] * 2)
            element = next(e for e in recorder.snapshots[0]['elements'] if e['selector'] == '#select')
            self.assertEqual(element['state_attributes'], {'aria-pressed': 'false'})

    def test_history_arguments_and_unbounded_attributes_are_rejected(self):
        for action in ('reload', 'back'):
            for target, value in (('/app', None), ('', 'data')):
                with self.assertRaises(ValueError):
                    Step(action, target, value)
        for name, value in [('value', 'secret'), ('href', '/app'), ('aria-busy', 'mixed'), ('aria-label', 'text')]:
            with self.assertRaises(ValueError):
                Check('attribute', '#element', value, name)
        for path in ('https://other.test/app', '//other.test/app', '/app?token=secret', '/app#fragment'):
            with self.assertRaises(ValueError):
                Check('url_path', '', path)
        with self.assertRaises(PolicyError):
            replace(history_plan('https://example.test'), expected=Check('url_path', '', '/outside')).validate_scope(local_policy('https://example.test'))
