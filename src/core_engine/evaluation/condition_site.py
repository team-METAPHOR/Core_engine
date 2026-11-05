"""Synthetic role/browser fixtures; bug flags stay server-side and out of model inputs."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from core_engine import BrowserPlan, Check, RunPolicy, Step


HTML = b'''<!doctype html><meta charset="utf-8"><title>Report preview</title>
<h1>Report preview</h1><button id="preview">Preview report</button>
<div id="status">Idle</div><div id="result">No preview</div>
<script>window.fixtureActions=[]; document.querySelector('#preview').onclick = async () => {
 window.fixtureActions.push('preview');
 document.querySelector('#status').textContent = 'Loading';
 const response = await fetch('/app/api/preview');
 document.querySelector('#result').textContent = response.status === 200 ? 'Preview ready' : 'Unavailable';
 document.querySelector('#status').textContent = 'Complete';
};</script>'''


def response_plan(url):
    return BrowserPlan((Step('navigate', url), Step('click', '#preview')),
        ready=Check('text', '#status', 'Complete'),
        expected=Check('response_status', '/app/api/preview', '200', 'GET'),
        symptom=Check('response_status', '/app/api/preview', '403', 'GET'))


@contextmanager
def condition_site(buggy=True):
    state = {'buggy': buggy}
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            role = self.headers.get('Authorization', '').removeprefix('Bearer synthetic-')
            requests.append({'path': self.path, 'role': role})
            status, body = 200, HTML
            if self.path == '/app/api/preview':
                status = 401 if role not in {'staff', 'admin'} else 403 if state['buggy'] and role == 'staff' else 200
                body = b'{}'
            elif self.path != '/app':
                status, body = 404, b''
            self.send_response(status)
            self.send_header('Content-Type', 'application/json' if self.path.endswith('/preview') else 'text/html')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    origin = f'http://127.0.0.1:{server.server_port}'
    policy = RunPolicy('demo', 1, origin, ('/app',), allow_private_targets=True, private_cidrs=('127.0.0.1/32',))
    try:
        yield origin, policy, state, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
