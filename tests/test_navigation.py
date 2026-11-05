"""Reaching the reported screen before acting, and questions a reporter can answer (issue #20, BUG-24)."""

import copy
import importlib.util
import tempfile
import unittest

from core_engine import BrowserOptions, JobStore, QARequest, QAService
from core_engine.planning import PlanValidationError, validate_candidate
from core_engine.qa_schema import plain_questions, repair_schema
from core_engine.scenario_contract import ground_contract
from fixture_site import site
from qa_fixtures import ScriptedModel, analysis, check, intake, screenshot
from test_transport import local_policy

REPORT = {'description': '무선 키보드 상품 페이지에서 장바구니 담기를 한 번 누르면 수량이 2로 표시됨',
          'expected_result': '수량 1 표시', 'test_data': ''}
SELECTOR_WORDS = ('selector', '셀렉터', '선택자', 'css', '후보', 'candidate', 'data-testid', 'dom')


def home_page(origin):
    def element(selector, tag, text, href=''):
        return {'selector': selector, 'tag': tag, 'role': '', 'name': '', 'text': text, 'enabled': True,
                'input_type': '', 'state_attributes': {}, 'href': href}
    return {'url': origin + '/app/shop', 'elements': [
        element('h1', 'h1', 'BugLab 쇼핑'), element('#notice', 'a', '공지사항', '/app/notice'),
        element('#card-keyboard', 'a', '무선 키보드 39,000원', '/app/shop/keyboard'),
        element('#card-monitor', 'a', '27인치 모니터 259,000원', '/app/shop/monitor')]}


def cart_plan(origin, *pre):
    return {'status': 'ready', 'questions': [], 'rationale': '상세 페이지에서 담기 1회', 'plan': {
        'steps': [{'action': 'navigate', 'target': origin + '/app/shop', 'value': ''}, *pre,
                  {'action': 'click', 'target': '#add-to-cart', 'value': ''}],
        'ready': check('#cart-ready', '담김'), 'expected': check('#cart-count', '1'),
        'symptom': check('#cart-count', '2')}}


def cart_intake():
    data = intake()
    data.update(title='담기 1회에 수량 2', required_actions=[
        {'action': 'click', 'value': '', 'source_quote': '장바구니 담기'}], actions_complete=True)
    return data


CARD_CLICK = {'action': 'click', 'target': '#card-keyboard', 'value': ''}


class PlanReachesReportedScreenTests(unittest.TestCase):
    origin = 'https://shop.test'

    def validate(self, generated, page):
        contract = ground_contract(cart_intake(), REPORT)
        return validate_candidate(generated['plan'], REPORT, entry_url=self.origin + '/app/shop',
                                  timeout_ms=3000, contract=contract, page=page)

    def test_action_on_another_screen_without_navigation_is_rejected(self):
        with self.assertRaises(PlanValidationError) as error:
            self.validate(cart_plan(self.origin), home_page(self.origin))
        self.assertEqual(error.exception.code, 'PLAN_SCREEN_NOT_REACHED')
        self.assertIn('page.elements', error.exception.feedback)

    def test_click_on_matching_card_or_navigate_to_seen_link_reaches_the_screen(self):
        plan = self.validate(cart_plan(self.origin, CARD_CLICK), home_page(self.origin))
        self.assertEqual([s.target for s in plan.steps][1:], ['#card-keyboard', '#add-to-cart'])
        seen = {'action': 'navigate', 'target': self.origin + '/app/shop/keyboard', 'value': ''}
        plan = self.validate(cart_plan(self.origin, seen), home_page(self.origin))
        self.assertEqual(plan.steps[1].target, self.origin + '/app/shop/keyboard')

    def test_reaching_click_is_not_an_added_interaction_in_a_complete_sequence(self):
        contract = {**ground_contract(cart_intake(), REPORT), 'actions_complete': True}
        validate_candidate(cart_plan(self.origin, CARD_CLICK)['plan'], REPORT, entry_url=self.origin + '/app/shop',
                           timeout_ms=3000, contract=contract, page=home_page(self.origin))

    def test_navigation_to_a_path_not_seen_on_the_entry_page_stays_unsupported(self):
        unseen = {'action': 'navigate', 'target': self.origin + '/app/admin', 'value': ''}
        with self.assertRaises(PlanValidationError) as error:
            self.validate(cart_plan(self.origin, unseen), home_page(self.origin))
        self.assertEqual(error.exception.code, 'PLAN_UNSUPPORTED_NAVIGATION')

    def test_without_page_or_screen_mention_the_plan_is_unchanged(self):
        self.validate(cart_plan(self.origin), None)
        report = {**REPORT, 'description': '장바구니 담기를 한 번 누르면 수량이 2로 표시됨'}
        validate_candidate(cart_plan(self.origin)['plan'], report, entry_url=self.origin + '/app/shop',
                           timeout_ms=3000, page=home_page(self.origin))


class PlainQuestionTests(unittest.TestCase):
    def test_selector_questions_become_one_intent_question(self):
        questions = plain_questions([
            "무선 키보드 상품 카드 안의 '장바구니 담기' 버튼에 대응하는 셀렉터가 후보 중 어느 것인지 확인이 필요합니다.",
            'Which candidate CSS selector [data-testid="add"] is right?',
            '쿠폰은 몇 번 적용했나요?'])
        self.assertEqual(questions, ['어느 화면에서 어떤 상품으로 이 동작을 했는지 알려주세요.', '쿠폰은 몇 번 적용했나요?'])
        for question in questions:
            self.assertFalse(any(word in question.lower() for word in SELECTOR_WORDS), question)

    def test_prompts_forbid_selector_questions(self):
        from core_engine.critic import REVIEW_PROMPT
        from core_engine.qa_schema import INTAKE_PROMPT, PLAN_PROMPT, REPAIR_PROMPT
        for prompt in (INTAKE_PROMPT, PLAN_PROMPT, REPAIR_PROMPT, REVIEW_PROMPT):
            self.assertIn('Never ask about selectors', prompt)

    def test_repair_schema_offers_one_navigation_step(self):
        self.assertIn('navigate_first', repair_schema()['properties']['status']['enum'])


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'install browser extra')
class NavigationBrowserTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = JobStore(self.temp.name)

    async def run_case(self, origin, responses, report=REPORT):
        model = ScriptedModel(responses)
        service = QAService(model, local_policy(origin), self.store, options=BrowserOptions(step_timeout_ms=300))
        request = QARequest('report-cart', 'demo', origin + '/app/shop', screenshot(), report['description'],
                            report['expected_result'], report['test_data'])

        async def reset(_):
            return True
        return await service.run(request, reset=reset, is_authorized=lambda: True), model

    async def test_plan_without_navigation_is_revised_to_open_the_detail_page(self):
        with site() as (origin, _, _):
            result, model = await self.run_case(origin, [
                cart_intake(), cart_plan(origin), cart_plan(origin, CARD_CLICK), analysis()])
            self.assertEqual(result.status, 'completed', result)
            self.assertEqual(result.report['plan_validation'][0]['code'], 'PLAN_SCREEN_NOT_REACHED')
            self.assertIn('/app/shop/keyboard', [e['href'] for e in model.calls[1]['context']['page']['elements']])
            self.assertEqual([s['target'] for s in result.report['scenario']['steps']][1:],
                             ['#card-keyboard', '#add-to-cart'])
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')

    async def test_repair_inserts_one_navigation_click_when_target_is_missing(self):
        # No screen phrase, so the nav-less plan passes validation and fails on the entry page.
        report = {**REPORT, 'description': '무선 키보드 장바구니 담기를 한 번 누르면 수량이 2로 표시됨'}
        with site() as (origin, _, _):
            original = cart_plan(origin)
            repair = {'status': 'navigate_first', 'target': '#card-keyboard',
                      'reason': '무선 키보드 상세로 이동 후 담기', 'questions': []}
            result, model = await self.run_case(origin, [cart_intake(), original, repair, analysis()], report)
            self.assertEqual(result.status, 'completed', result)
            self.assertEqual(result.report['run']['verdict']['value'], 'reproduced')
            scenario = result.report['scenario']
            self.assertEqual([s['target'] for s in scenario['steps']][1:], ['#card-keyboard', '#add-to-cart'])
            for key in ('ready', 'expected', 'symptom'):
                self.assertEqual(scenario[key], original['plan'][key])
            [record] = result.report['repairs']
            self.assertEqual((record['kind'], record['step'], record['inserted']['target']),
                             ('navigation', 1, '#card-keyboard'))

    async def test_navigation_repair_must_lead_toward_the_reported_item(self):
        report = {**REPORT, 'description': '무선 키보드 장바구니 담기를 한 번 누르면 수량이 2로 표시됨'}
        for target in ('#notice', '#card-monitor', '#invented'):
            with self.subTest(target=target), site() as (origin, _, _):
                repair = {'status': 'navigate_first', 'target': target, 'reason': '이동', 'questions': []}
                result, _ = await self.run_case(origin, [cart_intake(), cart_plan(origin), repair], report)
                self.assertEqual(result.error_code, 'MODEL_INVALID_OUTPUT', result)
                self.assertEqual(result.report['repairs'], [])

    async def test_repair_questions_never_ask_for_selectors(self):
        report = {**REPORT, 'description': '무선 키보드 장바구니 담기를 한 번 누르면 수량이 2로 표시됨'}
        with site() as (origin, _, _):
            repair = {'status': 'needs_info', 'target': '', 'reason': '대상 불명확', 'questions': [
                "무선 키보드 상품 카드 안의 실제 '장바구니 담기' 버튼에 대응하는 셀렉터가 후보 중 어느 것인지 확인이 필요합니다."]}
            result, _ = await self.run_case(origin, [cart_intake(), cart_plan(origin), repair], report)
            self.assertEqual(result.status, 'needs_info', result)
            self.assertTrue(result.questions)
            for question in [*result.questions, *result.report['questions']]:
                self.assertFalse(any(word in question.lower() for word in SELECTOR_WORDS), question)


if __name__ == '__main__':
    unittest.main()
