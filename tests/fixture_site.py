from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading


@contextmanager
def site():
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
                self.send_header("Location", "/outside")
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
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", hits, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
