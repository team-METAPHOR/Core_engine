"""Screenshot → plan → real Chromium QA → Markdown/JSON report; offline model by default."""

import argparse
import asyncio
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import threading

from core_engine import (BrowserOptions, BrowserRunner, Generation, JobStore, OpenAIModel,
                         QARequest, QAService, Recorder, RunPolicy)
from core_engine.qa_schema import decode_plan
from analyze_report import FixtureModel


def fixture_plan(url, broken=False):
    def check(selector, value):
        return {'kind': 'text', 'selector': selector, 'value': value, 'other_selector': ''}
    return {'steps': [
        {'action': 'navigate', 'target': url, 'value': ''},
        {'action': 'fill', 'target': '#query', 'value': 'cat'},
        {'action': 'click', 'target': '#old-search' if broken else '#search', 'value': ''},
        {'action': 'assert_text', 'target': '#result', 'value': 'cat'},
        {'action': 'fill', 'target': '#query', 'value': 'missing'},
        {'action': 'click', 'target': '#search', 'value': ''}],
        'ready': check('#ready', 'done'), 'expected': check('#result', 'empty'), 'symptom': check('#result', 'cat')}


class DemoModel:
    """Canned contract responses; never presents itself as AI reasoning."""
    def __init__(self, url, repair=False):
        self.url, self.repair = url, repair

    def generate(self, *, instructions, context, schema, images=()):
        properties = schema['properties']
        if 'observed_facts' in properties:
            value = {'status': 'ready', 'title': '빈 검색 결과에서 이전 목록이 남음', 'classification': 'functional',
                     'observed_facts': ['검색 화면에 cat 결과가 표시됨'], 'hypotheses': ['빈 결과 처리 누락 가능성'],
                     'expected_behavior': '결과가 없으면 empty 표시', 'expectation_source': 'report',
                     'symptom_description': 'missing 검색 완료 후 cat 결과가 남음',
                     'reproduction_steps': ['cat 검색', 'missing 검색'],
                     'preconditions': ['cat은 결과가 있고 missing은 결과가 없는 합성 데이터'], 'questions': []}
        elif 'plan' in properties:
            value = {'status': 'ready', 'questions': [], 'rationale': '합성 검색 데이터로 이전 목록 잔존 여부 확인',
                     'plan': fixture_plan(self.url, self.repair)}
        elif 'target' in properties:
            value = {'status': 'repair', 'target': '#search', 'reason': '실제 검색 버튼 ID로 대상만 보정', 'questions': []}
        else:
            return FixtureModel().generate(instructions=instructions, context=context, schema=schema)
        return Generation(json.dumps(value, ensure_ascii=False), 'offline-fixture')


async def execute(origin, store, state, args, live_model):
    url = origin + '/catalog'
    policy = RunPolicy('local-demo', 1, origin, ('/catalog',), allow_private_targets=True,
                       private_cidrs=('127.0.0.1/32',))
    options = BrowserOptions(app_version='fixture-bug', step_timeout_ms=500 if not args.live else 3000)
    async def reset(_attempt):
        # Fixture has no server state; every fresh browser context starts empty.
        return True

    # Produce the reporter image using a real guarded browser run, not a generated mock screenshot.
    source_job = store.create()
    source_recorder = Recorder(store, source_job, options)
    await BrowserRunner(policy, options=options, recorder=source_recorder).run(
        decode_plan(fixture_plan(url)), reset=reset, is_authorized=lambda: True)
    screenshot = source_recorder.image(source_recorder.snapshots[-1])
    request = QARequest('search-report', 'local-demo', url, screenshot,
                        'cat 검색 후 missing 검색 시 이전 cat 목록이 남습니다. 완료 표시는 done입니다.',
                        'missing 검색 완료 후 결과 영역에 empty가 표시되어야 합니다.',
                        'cat은 결과가 있는 검색어, missing은 결과가 없는 검색어입니다.')
    model = live_model if args.live else DemoModel(url, args.repair)
    service = QAService(model, policy, store, options=options)
    result = await service.run(request, reset=reset, is_authorized=lambda: True)
    outputs = [result]
    if args.retest_fixed and result.report['run']:
        # Switch only the synthetic fixture build; the QA engine does not modify customer code.
        state['fixed'] = True
        from dataclasses import replace
        service = QAService(model, policy, store, options=replace(options, app_version='fixture-fixed'))
        outputs.append(await service.retest(result.job_id, reset=reset, is_authorized=lambda: True))
    for item in outputs:
        run = item.report['run']
        print(json.dumps({'mode': 'openai' if args.live else 'fixture (실제 AI 추론 아님)',
                          'job_id': item.job_id, 'status': item.status,
                          'verdict': run['verdict']['value'] if run else None,
                          'regression': item.report['regression'], 'error_code': item.error_code,
                          'report': str(store.root / item.job_id / 'report.md'),
                          'json': str(store.root / item.job_id / 'result.json')}, ensure_ascii=False))
    if any(item.status in {'failed', 'cancelled', 'inconclusive'} for item in outputs):
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', default='.qa-results', help='비공개 결과 저장 경로')
    parser.add_argument('--live', action='store_true', help='실제 유료 OpenAI 이미지 분석 호출')
    parser.add_argument('--repair', action='store_true', help='고정 모델이 잘못된 버튼 ID를 제안했다가 보정하는 데모')
    parser.add_argument('--retest-fixed', action='store_true', help='수정된 합성 화면에서 같은 시나리오로 재검증')
    args = parser.parse_args()
    try:
        live_model = OpenAIModel.from_env() if args.live else None
    except ValueError as exc:
        parser.error(str(exc))
    state = {'fixed': False}
    source = Path(__file__).with_name('search_bug.html').read_text()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass
        def do_GET(self):
            payload = source.replace("const fixed = new URLSearchParams(location.search).get('fixed') === '1';",
                                     'const fixed = ' + str(state['fixed']).lower() + ';').encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        asyncio.run(execute(f'http://127.0.0.1:{server.server_port}', JobStore(args.output), state, args, live_model))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == '__main__':
    main()
