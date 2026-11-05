"""Plan targets must be CSS selectors: plain visible text is grounded in the snapshot or rejected."""
import unittest

from core_engine.planning import PlanValidationError, plain_text_target, validate_candidate
from qa_fixtures import check

ENTRY = 'https://shop.test/app/cart'
REPORT = {'description': '장바구니에서 수량 늘리기 버튼을 눌러도 수량이 늘어나지 않습니다', 'expected_result': '', 'test_data': ''}
PAGE = {'elements': [
    {'selector': '[data-testid="cart-link"]', 'tag': 'a', 'role': '', 'name': '', 'text': '장바구니', 'href': '/app/cart'},
    {'selector': '[data-testid="quantity-decrease"]', 'tag': 'button', 'role': '', 'name': '수량 줄이기', 'text': '-', 'href': ''},
    {'selector': '[data-testid="quantity-increase"]', 'tag': 'button', 'role': '', 'name': '수량 늘리기', 'text': '+', 'href': ''},
    {'selector': '[data-testid="quantity"]', 'tag': 'span', 'role': '', 'name': '', 'text': '수량 1', 'href': ''},
    {'selector': '#status', 'tag': 'p', 'role': '', 'name': '', 'text': '준비 완료', 'href': ''},
]}


def proposal(target, symptom='[data-testid="quantity"]'):
    return {'steps': [{'action': 'navigate', 'target': ENTRY, 'value': ''},
                      {'action': 'click', 'target': target, 'value': ''}],
            'ready': check('#status', '준비 완료'), 'expected': check('[data-testid="quantity"]', '수량 2'),
            'symptom': check(symptom, '수량 1')}


def validate(data, page=PAGE, **kwargs):
    return validate_candidate(data, REPORT, entry_url=ENTRY, timeout_ms=3000, targets=page, **kwargs)


class TextTargetTests(unittest.TestCase):
    def test_plain_text_is_told_apart_from_css(self):
        for text in ('수량 늘리기 버튼', '수량 표시', 'Add to cart', '장바구니'):
            self.assertTrue(plain_text_target(text), text)
        for selector in ('#a', 'button', 'div > button', '[data-testid="add-to-cart"]', 'ul li:nth-of-type(2) a',
                         'my-widget', '.price', 'main section p'):
            self.assertFalse(plain_text_target(selector), selector)

    def test_text_click_target_resolves_to_matching_element_selector(self):
        plan = validate(proposal('수량 늘리기 버튼'))
        self.assertEqual(plan.steps[1].target, '[data-testid="quantity-increase"]')

    def test_exact_text_match_beats_containment(self):
        self.assertEqual(validate(proposal('장바구니')).steps[1].target, '[data-testid="cart-link"]')
        self.assertEqual(validate(proposal('수량 줄이기')).steps[1].target, '[data-testid="quantity-decrease"]')

    def test_text_check_selector_resolves_too(self):
        plan = validate(proposal('[data-testid="quantity-increase"]', symptom='수량 1'))
        self.assertEqual(plan.symptom.selector, '[data-testid="quantity"]')

    def test_unmatched_text_target_is_rejected_with_specific_code(self):
        for data, field in ((proposal('쿠폰 적용 버튼'), 'steps.1.target'),
                            (proposal('#status', symptom='배송비 표시'), 'symptom.selector')):
            with self.subTest(field=field), self.assertRaises(PlanValidationError) as error:
                validate(data)
            self.assertEqual((error.exception.code, error.exception.field), ('PLAN_TEXT_TARGET', field))
        with self.assertRaises(PlanValidationError) as error:
            validate(proposal('수량 늘리기 버튼'), page=None)
        self.assertEqual(error.exception.code, 'PLAN_TEXT_TARGET')

    def test_last_proposal_may_keep_unmatched_text_for_runtime_repair(self):
        plan = validate(proposal('쿠폰 적용 버튼'), allow_text_targets=True)
        self.assertEqual(plan.steps[1].target, '쿠폰 적용 버튼')

    def test_css_targets_are_untouched(self):
        plan = validate(proposal('[data-testid="quantity-increase"]'))
        self.assertEqual(plan.steps[1].target, '[data-testid="quantity-increase"]')


if __name__ == '__main__':
    unittest.main()
