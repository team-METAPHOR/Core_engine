import copy
from io import BytesIO
import json

from PIL import Image

from core_engine import Generation, Screenshot, TokenUsage
from core_engine.qa_schema import PLAN_SCHEMA, draft_plan


def screenshot():
    buffer = BytesIO()
    Image.new('RGB', (320, 320), 'white').save(buffer, format='PNG')
    return Screenshot(buffer.getvalue())


def intake(status='ready'):
    return {'status': status, 'title': '빈 검색 결과에서 이전 목록 유지', 'classification': 'functional',
            'observed_facts': ['검색 결과가 표시됨'], 'hypotheses': ['빈 결과 처리 누락 가능성'],
            'expected_behavior': '없는 검색어에는 빈 목록 표시', 'expectation_source': 'report',
            'symptom_description': '이전 cat 목록이 남음', 'reproduction_steps': ['cat 검색 후 missing 검색'],
            'preconditions': ['cat은 결과 있음, missing은 결과 없음'],
            'required_actions': [], 'actions_complete': False, 'completion_signal': None,
            'questions': ['기대 동작을 알려주세요.'] if status == 'needs_info' else []}


def check(selector, value, kind='text', other=''):
    return {'kind': kind, 'selector': selector, 'value': value, 'other_selector': other}


def plan(url, broken=False):
    return {'status': 'ready', 'questions': [], 'rationale': '정상 검색 다음 빈 검색 결과 확인', 'plan': {
        'steps': [
            {'action': 'navigate', 'target': url, 'value': ''},
            {'action': 'fill', 'target': '#query', 'value': 'cat'},
            {'action': 'click', 'target': '#obsolete-search' if broken else '#search', 'value': ''},
            {'action': 'assert_text', 'target': '#result', 'value': 'cat'},
            {'action': 'fill', 'target': '#query', 'value': 'missing'},
            {'action': 'click', 'target': '#search', 'value': ''}],
        'ready': check('#ready', 'done'), 'expected': check('#result', 'empty'), 'symptom': check('#result', 'cat')}}


def analysis():
    return {'classification': 'functional', 'confidence': 'medium', 'classification_reason': '검색 결과 갱신 문제',
            'severity': 'low', 'severity_reason': '화면 결과 혼동 가능', 'summary': '검토가 필요한 검색 결과 표시 문제입니다.',
            'hypotheses': [{'description': '빈 응답 처리 누락 가능성', 'source_refs': ['report']}],
            'solutions': [{'title': '빈 결과 갱신', 'description': '빈 결과에도 목록 상태 갱신을 검토합니다.',
                           'source_refs': ['report'], 'verification_steps': ['결과 있음 다음 빈 검색 확인'],
                           'tradeoffs': ['로딩 상태와 구분 필요']}], 'missing_information': ['관련 소스 코드']}


class ScriptedModel:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        if not self.responses:
            raise AssertionError('unexpected model call')
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            response = response(kwargs)
        response = copy.deepcopy(response)
        if 'plan' in kwargs.get('schema', {}).get('properties', {}) and response.get('plan') is not None:
            response['plan'] = draft_plan(response['plan'])
        return Generation(json.dumps(response), 'offline-fixture', TokenUsage(100, 100))
