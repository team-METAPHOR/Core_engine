from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs
import threading
from core_engine.evaluation.local_tls import local_https

LOGIN_ID = 'qa-member-login-canary'
PASSWORD = 'private-login-password-canary-47'


@contextmanager
def login_site():
    state = {'logins': 0, 'sessions': [], 'hits': [], 'fixed': False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def send(self, status, body='', headers=()):
            payload = body.encode()
            self.send_response(status)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            for name, value in headers:
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            state['hits'].append(('POST', self.path))
            if self.path != '/app/auth':
                self.send(400)
                return
            values = parse_qs(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
            valid = (values.get('username') == [LOGIN_ID] and values.get('password') == [PASSWORD]
                     and values.get('csrf') == ['fixture-csrf'] and 'csrf=fixture-csrf' in self.headers.get('Cookie', ''))
            if not valid:
                self.send(401, '<p>' + PASSWORD + '</p>')
                return
            state['logins'] += 1
            token = 'synthetic-session-' + str(state['logins'])
            state['sessions'].append(token)
            self.send(303, headers=[('Location', '/app/home'),
                ('Set-Cookie', 'session=' + token + '; Path=/app; HttpOnly; SameSite=Lax'),
                ('Set-Cookie', 'preference=test; Path=/app; SameSite=Lax')])

        def do_GET(self):
            state['hits'].append(('GET', self.path))
            if self.path == '/app/login':
                self.send(200, '''<!doctype html><style>body {background:rgb(180,0,0)}</style>
<h1>LOGIN SCREEN</h1><form action="/app/auth" method="post">
<input name="username" autocomplete="username"><input name="password" type="password">
<input type="hidden" name="csrf" value="fixture-csrf"><button type="submit">Sign in</button></form>
<script>console.error('login phase must not be captured');</script>''',
                    [('Set-Cookie', 'csrf=fixture-csrf; Path=/app; HttpOnly')])
                return
            cookie = self.headers.get('Cookie', '')
            if not any('session=' + token in cookie for token in state['sessions']):
                self.send(303, headers=[('Location', '/app/login')])
                return
            if self.path == '/app/home':
                self.send(200, '<!doctype html><h1 id="account">Signed in</h1>')
                return
            result = 'normal' if state['fixed'] else 'broken'
            leak = '<p id="echo">' + PASSWORD + '</p>' if self.path == '/app/echo' else ''
            self.send(200, '<!doctype html><div id="ready">done</div><div id="result">' + result + '</div>' + leak +
                '<button id="write" onclick="fetch(\'/app/write\', {method:\'POST\',body:\'not-allowed\'}).catch(()=>{})">Write</button>')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    with local_https(server):
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        try:
            yield f'https://127.0.0.1:{server.server_port}', state
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
