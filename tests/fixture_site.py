from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from core_engine.evaluation.local_tls import local_https


@contextmanager
def site(*, tls=False):
    hits = []
    state = {"fixed": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            hits.append(("POST", self.path, self.headers.get("Host")))
            self.send_response(200)
            self.end_headers()

        def do_GET(self):
            hits.append(("GET", self.path, self.headers.get("Host")))
            if self.path == "/app/redirect":
                self.send_response(302)
                self.send_header("Location", state.get("redirect_location", "/outside"))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path == "/app/privacy":
                payload = b'''<!doctype html><div id="outer">public <span data-private id="private">customer-canary</span></div>
<input id="password" type="password" value="password-canary"><input id="query" value="input-canary">
<div id="email">private@example.com</div><button id="search">Search</button>
<script>console.error('Authorization: Bearer console-canary');</script>'''
            elif self.path in {"/app/visual", "/app/visual-fixed"}:
                left = 150 if self.path.endswith("fixed") else 30
                payload = (f'''<!doctype html><style>#a,#b {{ position:absolute;top:20px;width:100px;height:40px; }}
#a {{left:10px}} #b {{left:{left}px}} #clip {{position:absolute;top:100px;left:0;width:20px;white-space:nowrap;overflow:hidden}}</style>
<button id="a">First</button><button id="b">Second</button><div id="clip">overflowing content</div><div id="ready">done</div>''').encode()
            elif self.path == "/app/shop":
                # Entry page: product cards only; the add-to-cart button lives on the detail page.
                payload = '''<!doctype html><meta charset="utf-8"><h1>BugLab 쇼핑</h1>
<a id="notice" href="/app/notice">공지사항</a>
<ul><li><a id="card-keyboard" href="/app/shop/keyboard">무선 키보드 39,000원</a></li>
<li><a id="card-monitor" href="/app/shop/monitor">27인치 모니터 259,000원</a></li></ul>'''.encode()
            elif self.path == "/app/shop/keyboard":
                fixed = "true" if state["fixed"] else "false"
                payload = f'''<!doctype html><meta charset="utf-8"><h1>무선 키보드</h1>
<button id="add-to-cart">장바구니 담기</button><div id="cart-count">0</div><div id="cart-ready">대기</div>
<script>document.querySelector('#add-to-cart').onclick = () => {{
  document.querySelector('#cart-count').textContent = {fixed} ? '1' : '2';
  document.querySelector('#cart-ready').textContent = '담김';
}};</script>'''.encode()
            elif self.path == "/app/large":
                payload = b"x" * 2000
            elif self.path == "/app/truncated":
                self.send_response(200)
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b"short")
                self.close_connection = True
                return
            else:
                attack = ""
                if self.path == "/app/post":
                    attack = "fetch('/app/write', {method:'POST', body:'test'}).catch(()=>{});"
                elif self.path == "/app/outside":
                    attack = "fetch('/outside').catch(()=>{});"
                elif self.path == "/app/websocket":
                    attack = "new WebSocket('ws://' + location.host + '/app/ws');"
                fixed = self.path == "/app/fixed" or (self.path == "/app/mixed" and state["fixed"])
                payload = ("""<!doctype html><meta charset="utf-8">
<input id="query"><button id="search">Search</button>
<div id="result">empty</div><div id="ready">ready</div><div id="fresh"></div>
<script>
document.querySelector('#fresh').textContent = localStorage.getItem('ran') ? 'no' : 'yes';
localStorage.setItem('ran', '1');
document.querySelector('#search').onclick = () => {
  const q = document.querySelector('#query').value;
  if (q === 'cat') document.querySelector('#result').textContent = 'cat';
  FIX
  document.querySelector('#ready').textContent = 'done';
};
ATTACK
</script>""".replace("FIX", "else document.querySelector('#result').textContent = 'empty';" if fixed else "")
                    .replace("ATTACK", attack)).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    with local_https(server) if tls else nullcontext():
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
        thread.start()
        try:
            scheme = "https" if tls else "http"
            yield f"{scheme}://127.0.0.1:{server.server_port}", hits, state
        finally:
            server.shutdown()
            server.server_close()
            thread.join()
