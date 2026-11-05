"""Loopback-only synthetic apps. Variant selection lives on the host, not in URLs."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from core_engine import RunPolicy


STYLE = """body{font:20px sans-serif;margin:48px;color:#172033;background:#f8fafc}
main{background:white;padding:32px;max-width:800px;border:1px solid #ddd;border-radius:12px}
button,input{font:inherit;padding:12px;margin:8px 8px 8px 0}#status{margin-top:24px;color:#465}
#results{margin-top:20px;min-height:32px}h1{font-size:28px}label{display:block}"""


class FixtureSite:
    def __init__(self, family):
        self.family = family
        self.variant = "issue"
        self.page_path = "/page/" + uuid4().hex
        self.sessions = {}
        self.requests = []
        self.lock = threading.Lock()

    def activate(self, variant):
        with self.lock:
            self.variant = variant
            self.sessions.clear()
            self.requests.clear()

    @property
    def url(self):
        return self.origin + self.page_path

    def policy(self):
        return RunPolicy("qa-evaluation", 1, self.origin, (self.page_path, "/activity"),
                         allow_private_targets=True, private_cidrs=("127.0.0.1/32",))

    def page(self):
        session_id = uuid4().hex
        initial = "검색 결과 없음" if self.family == "search" else "티셔츠, 노트"
        self.sessions[session_id] = {"events": [], "value": initial}
        script = "window.qaSession = " + json.dumps(session_id) + ";"
        if self.family in {"search", "filter"}:
            if self.family == "search":
                title = "상품 검색"
                content = '<label for="query">검색어</label><input id="query"><button id="search">검색</button>'
                bindings = "document.querySelector('#search').onclick=()=>update('search',document.querySelector('#query').value);"
            else:
                title = "상품 목록"
                content = '<button id="category">의류만 보기</button><button id="clear">필터 초기화</button>'
                bindings = "document.querySelector('#category').onclick=()=>update('category','의류');document.querySelector('#clear').onclick=()=>update('clear','');"
            content += '<div id="results">' + initial + '</div><div id="status">준비 완료</div>'
            script += """
async function update(op,value){
 document.querySelector('#status').textContent='처리 중';
 const reply=await fetch('/activity/'+window.qaSession+'?op='+encodeURIComponent(op)+'&value='+encodeURIComponent(value));
 const data=await reply.json();
 document.querySelector('#results').textContent=data.value;
 document.querySelector('#status').textContent='완료';
}
""" + bindings
        elif self.family == "clipping":
            title = "상품 안내"
            width = 155 if self.variant == "issue" else 720
            content = (f'<div id="headline" style="width:{width}px;white-space:nowrap;overflow:hidden;font-size:28px">'
                       '가볍고 따뜻한 여행용 재킷 상품 안내</div><div id="status">준비 완료</div>')
        elif self.family == "overlap":
            title = "페이지 이동"
            left = 95 if self.variant == "issue" else 260
            content = ('<div style="position:relative;height:100px">'
                       '<button id="previous" style="position:absolute;left:0;width:210px">이전 페이지</button>'
                       f'<button id="next" style="position:absolute;left:{left}px;width:210px">다음 페이지</button>'
                       '</div><div id="status">준비 완료</div>')
        else:
            title = "계정 정보"
            content = '<p id="profile">이름: 테스트 사용자</p><p>알림: 사용</p><button>설정 보기</button><div id="status">준비 완료</div>'
        return (f'<!doctype html><html lang="ko"><meta charset="utf-8"><title>{title}</title>'
                f'<style>{STYLE}</style><main><h1>{title}</h1>{content}</main><script>{script}</script></html>').encode()

    def respond(self, path):
        parsed = urlsplit(path)
        with self.lock:
            status, content_type = 200, "text/html; charset=utf-8"
            if parsed.path == self.page_path:
                if self.variant in {"unavailable", "unauthorized"}:
                    status = 503 if self.variant == "unavailable" else 401
                    body = b"Service unavailable" if status == 503 else b"Authentication required"
                else:
                    body = self.page()
            elif parsed.path.startswith("/activity/"):
                session_id = parsed.path.rsplit("/", 1)[-1]
                data = self.sessions.get(session_id)
                query = parse_qs(parsed.query, keep_blank_values=True)
                op, value = query.get("op", [""])[0], query.get("value", [""])[0]
                if data is None or op not in {"search", "category", "clear"}:
                    status, body = 404, b"Not found"
                else:
                    data["events"].append([op, value])
                    if self.family == "search" and op == "search":
                        if value == "머그":
                            data["value"] = "머그"
                        elif self.variant == "control":
                            data["value"] = "검색 결과 없음"
                    elif self.family == "filter":
                        if op == "category":
                            data["value"] = "티셔츠"
                        elif op == "clear" and self.variant == "control":
                            data["value"] = "티셔츠, 노트"
                    body = json.dumps({"value": data["value"]}, ensure_ascii=False).encode()
                    content_type = "application/json; charset=utf-8"
            else:
                status, body = 404, b"Not found"
            self.requests.append({"path": parsed.path, "status": status})
            return status, content_type, body

    async def observe_truth(self, page):
        """Independent host-owned oracle; this data never goes back to the planner."""
        values = await page.evaluate("""() => {
            const text=id=>document.getElementById(id)?.innerText.trim();
            const e=document.getElementById('headline');
            const a=document.getElementById('previous')?.getBoundingClientRect();
            const b=document.getElementById('next')?.getBoundingClientRect();
            return {session:window.qaSession, result:text('results'), status:text('status'),
                clipped:e ? e.scrollWidth>e.clientWidth+1 : null,
                overlap:a&&b ? Math.min(a.right,b.right)-Math.max(a.left,b.left)>1 &&
                              Math.min(a.bottom,b.bottom)-Math.max(a.top,b.top)>1 : null};
        }""")
        with self.lock:
            events = list(self.sessions.get(values.get("session"), {}).get("events", []))
        if self.family == "search":
            exercised = events[-2:] == [["search", "머그"], ["search", "없는상품"]] and values.get("status") == "완료"
            symptom = values.get("result") == "머그"
            normal = values.get("result") == "검색 결과 없음"
        elif self.family == "filter":
            exercised = events[-2:] == [["category", "의류"], ["clear", ""]] and values.get("status") == "완료"
            symptom = values.get("result") == "티셔츠"
            normal = values.get("result") == "티셔츠, 노트"
        else:
            exercised = values.get("status") == "준비 완료"
            symptom = values.get("clipped" if self.family == "clipping" else "overlap")
            normal = symptom is False
        return {"exercised": exercised, "symptom": symptom, "normal": normal, "actions": events}


@contextmanager
def fixture_site(family, *, site_factory=FixtureSite):
    site = site_factory(family)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            status, content_type, body = site.respond(self.path)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    site.origin = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.02), daemon=True)
    thread.start()
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
