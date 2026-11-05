import unittest

from core_engine import QARequest
from core_engine.qa_schema import flatten_draft
from core_engine.scenario_contract import ground_contract
from qa_fixtures import intake


class ReporterActivityTests(unittest.TestCase):
    def test_optional_context_is_grounded_without_changing_description(self):
        text = 'click /cart selector=[data-testid="increase"] label=수량 늘리기 feedback=수량 확인 완료'
        request = QARequest('r', 'p', 'https://example.com/cart', description='이거 안돼요',
                            reporter_activity=text)
        self.assertEqual(request.text()['description'], '이거 안돼요')
        self.assertEqual(request.text()['reporter_activity'], text)
        parsed = intake()
        parsed['required_actions'] = [{'action': 'click', 'value': '', 'source_quote': text}]
        contract = ground_contract(parsed, request.text())
        self.assertIsNotNone(contract)

    def test_old_request_and_bounded_context(self):
        self.assertNotIn('reporter_activity', QARequest('r', 'p', 'https://example.com',
                         description='문제').text())
        with self.assertRaises(ValueError):
            QARequest('r', 'p', 'https://example.com', description='문제', reporter_activity='x'*8193)

    def test_initial_route_does_not_erase_recorded_setup_navigation(self):
        navigate = {'action': 'navigate', 'target': 'https://example.com/products/2', 'value': ''}
        draft = {'setup_steps': [navigate], 'final_action': {'action': 'click', 'target': 'button', 'value': ''},
                 'ready': {}, 'expected': {}, 'symptom': {}}
        steps = flatten_draft(draft, entry_url='https://example.com/cart')['steps']
        self.assertEqual([s['target'] for s in steps[:2]],
                         ['https://example.com/cart', 'https://example.com/products/2'])
