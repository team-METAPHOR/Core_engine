"""Additional public development cases; never used as production planner context."""

import json
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from core_engine import BrowserPlan, Check, Step
from .cases import Case
from .site import FixtureSite, STYLE


SUITE_VERSION = "qa-transfer-v1"
REPORTS = {
    "activation": (
        "회원 정보에서 표시 이름에 샘플사용자를 입력해도 계속 버튼이 비활성 상태입니다. 입력 확인 완료가 처리 완료 표시입니다.",
        "표시 이름 샘플사용자를 입력하고 입력 확인 완료가 표시되면 계속 버튼이 활성화되어야 합니다.",
        "샘플사용자는 사용 가능한 합성 표시 이름입니다.",
    ),
    "quantity": (
        "장바구니 수량을 3으로 바꾸고 금액 갱신을 눌러도 합계가 7,500원에 머뭅니다. 계산 완료 표시 후 확인합니다.",
        "수량 3개에 대한 합계는 22,500원이어야 합니다.",
        "합성 상품 1개는 7,500원이고 배송비와 할인은 없습니다.",
    ),
    "dismissal": (
        "알림 화면에서 알림 닫기를 누르고 닫기 처리 완료가 표시되어도 안내 배너가 계속 보입니다.",
        "닫기 처리가 끝나면 안내 배너가 보이지 않아야 합니다.",
        "배너는 새 소식을 확인해 주세요라는 내용을 표시합니다.",
    ),
    "collection": (
        "비어 있는 보관 목록에서 항목 추가를 한 번 누르면 같은 항목이 두 개 생깁니다. 추가 완료 표시 후 확인합니다.",
        "한 번 추가한 뒤 보관 목록의 항목은 정확히 1개여야 합니다.",
        "목록은 비어 있는 상태에서 시작합니다. 합성 항목 이름은 보관 항목입니다.",
    ),
}
CASES = tuple(Case(f"{family}_{category}", family, category, variant, *report)
              for family, report in REPORTS.items()
              for category, variant in (("bug", "issue"), ("normal", "control")))


def reference_plan(family, url):
    steps = [Step("navigate", url)]
    if family == "activation":
        steps += [Step("fill", "#alias", "샘플사용자")]
        expected, symptom = Check("enabled", "#continue", "true"), Check("enabled", "#continue", "false")
        ready = "입력 확인 완료"
    elif family == "quantity":
        steps += [Step("fill", "#quantity", "3"), Step("click", "#recalculate")]
        expected, symptom = Check("text", "#total", "22,500원"), Check("text", "#total", "7,500원")
        ready = "계산 완료"
    elif family == "dismissal":
        steps += [Step("click", "#dismiss")]
        expected, symptom = Check("visible", "#notice", "false"), Check("visible", "#notice", "true")
        ready = "닫기 처리 완료"
    elif family == "collection":
        steps += [Step("click", "#add")]
        expected, symptom = Check("count", "#items li", "1"), Check("count", "#items li", "2")
        ready = "추가 완료"
    else:
        raise ValueError("unknown transfer family")
    return BrowserPlan(tuple(steps), Check("text", "#status", ready), symptom, expected=expected)


class TransferSite(FixtureSite):
    def page(self):
        session = uuid4().hex
        self.sessions[session] = {"events": [], "enabled": False, "total": "7,500원", "visible": True, "count": 0}
        if self.family == "activation":
            title = "회원 정보"
            body = '<label for="alias">표시 이름</label><input id="alias"><button id="continue" disabled>계속</button>'
            binding = "document.querySelector('#alias').oninput=()=>update('name',document.querySelector('#alias').value);"
            render = "document.querySelector('#continue').disabled=!data.enabled;"
        elif self.family == "quantity":
            title = "장바구니"
            body = ('<p>상품 가격: 7,500원</p><label for="quantity">수량</label><input id="quantity" type="number" value="1">'
                    '<button id="recalculate">금액 갱신</button><p>합계: <span id="total">7,500원</span></p>')
            binding = "document.querySelector('#recalculate').onclick=()=>update('quantity',document.querySelector('#quantity').value);"
            render = "document.querySelector('#total').textContent=data.total;"
        elif self.family == "dismissal":
            title = "알림"
            body = '<div id="notice">새 소식을 확인해 주세요</div><button id="dismiss">알림 닫기</button>'
            binding = "document.querySelector('#dismiss').onclick=()=>update('dismiss','');"
            render = "document.querySelector('#notice').hidden=!data.visible;"
        else:
            title = "보관 목록"
            body = '<button id="add">항목 추가</button><ul id="items" aria-label="보관 목록"></ul>'
            binding = "document.querySelector('#add').onclick=()=>update('add','');"
            render = "document.querySelector('#items').replaceChildren(...Array.from({length:data.count},()=>{const li=document.createElement('li');li.textContent='보관 항목';return li;}));"
        script = "window.qaSession=" + json.dumps(session) + ";"
        script += "async function update(op,value){document.querySelector('#status').textContent='처리 중';"
        script += "const response=await fetch('/activity/'+window.qaSession+'?op='+encodeURIComponent(op)+'&value='+encodeURIComponent(value));const data=await response.json();"
        script += render + "document.querySelector('#status').textContent=data.status;}" + binding
        return (f'<!doctype html><html lang="ko"><meta charset="utf-8"><title>{title}</title><style>{STYLE}</style>'
                f'<main><h1>{title}</h1>{body}<div id="status">준비 완료</div></main><script>{script}</script></html>').encode()

    def respond(self, path):
        parsed = urlsplit(path)
        if not parsed.path.startswith("/activity/"):
            return super().respond(path)
        with self.lock:
            data = self.sessions.get(parsed.path.rsplit("/", 1)[-1])
            query = parse_qs(parsed.query, keep_blank_values=True)
            op, value = query.get("op", [""])[0], query.get("value", [""])[0]
            required = {"activation": "name", "quantity": "quantity", "dismissal": "dismiss", "collection": "add"}[self.family]
            if data is None or op != required:
                status, body = 404, b"Not found"
            else:
                status = 200
                data["events"].append([op, value])
                control = self.variant == "control"
                if op == "name":
                    data.update(enabled=control and bool(value.strip()), status="입력 확인 완료")
                elif op == "quantity":
                    quantity = int(value) if value.isascii() and value.isdigit() and len(value) < 3 else 1
                    data.update(total=f"{7500 * min(quantity, 9):,}원" if control else "7,500원", status="계산 완료")
                elif op == "dismiss":
                    data.update(visible=not control, status="닫기 처리 완료")
                else:
                    data.update(count=data["count"] + (1 if control else 2), status="추가 완료")
                body = json.dumps({k: v for k, v in data.items() if k != "events"}, ensure_ascii=False).encode()
            self.requests.append({"path": parsed.path, "status": status})
            return status, "application/json; charset=utf-8", body

    async def observe_truth(self, page):
        values = await page.evaluate("""() => {
            const notice=document.getElementById('notice');
            return {session:window.qaSession, status:document.getElementById('status')?.innerText,
                enabled:document.getElementById('continue')?.disabled === false,
                total:document.getElementById('total')?.innerText,
                visible:!!notice && notice.getClientRects().length>0,
                count:document.querySelectorAll('#items li').length};
        }""")
        with self.lock:
            events = list(self.sessions.get(values.get("session"), {}).get("events", []))
        wanted, ready, symptom, normal = {
            "activation": ([["name", "샘플사용자"]], "입력 확인 완료", not values["enabled"], values["enabled"]),
            "quantity": ([["quantity", "3"]], "계산 완료", values.get("total") == "7,500원", values.get("total") == "22,500원"),
            "dismissal": ([["dismiss", ""]], "닫기 처리 완료", values["visible"], not values["visible"]),
            "collection": ([["add", ""]], "추가 완료", values["count"] == 2, values["count"] == 1),
        }[self.family]
        return {"exercised": events == wanted and values["status"] == ready,
                "symptom": symptom, "normal": normal, "actions": events}
