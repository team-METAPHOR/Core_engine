import copy
import unittest

from core_engine.analysis_schema import validate_output
from core_engine.planning import PlanValidationError, validate_candidate
from core_engine.qa_schema import PLAN_DATA_SCHEMA
from qa_fixtures import check, plan


class PlanContractTests(unittest.TestCase):
    def setUp(self):
        self.data = plan('https://example.test/products')['plan']
        self.report = {'description': 'Search cat, then missing.', 'expected_result': 'Empty results.', 'test_data': ''}

    def validate(self, data):
        validate_output(data, PLAN_DATA_SCHEMA)
        return validate_candidate(data, self.report, entry_url='https://example.test/products', timeout_ms=3000)

    def test_boolean_and_overlap_contract_applies_to_every_predicate(self):
        for role in ('ready', 'expected', 'symptom'):
            for kind in ('visible', 'enabled', 'clipped', 'overlap'):
                data = copy.deepcopy(self.data)
                data[role] = check('#a', '', kind, '#b' if kind == 'overlap' else '')
                with self.subTest(role=role, kind=kind), self.assertRaises(ValueError):
                    validate_output(data, PLAN_DATA_SCHEMA)
                data[role]['value'] = 'false'
                validate_output(data, PLAN_DATA_SCHEMA)

    def test_readiness_cannot_depend_on_either_outcome(self):
        for role in ('expected', 'symptom'):
            data = copy.deepcopy(self.data)
            data['ready'] = data[role]
            with self.assertRaises(PlanValidationError) as error:
                self.validate(data)
            self.assertEqual(error.exception.code, 'PLAN_READINESS_IS_OUTCOME')

    def test_input_must_preserve_submitted_literal_but_can_be_cleared(self):
        self.validate(self.data)
        self.data['steps'][4]['value'] = 'missing-item'
        with self.assertRaises(PlanValidationError) as error:
            self.validate(self.data)
        self.assertEqual(error.exception.code, 'PLAN_UNSUPPORTED_INPUT')
        self.data['steps'][4]['value'] = ''
        self.validate(self.data)

    def test_static_layout_plan_requires_no_artificial_interaction(self):
        data = {'steps': self.data['steps'][:1], 'ready': check('#card', 'true', 'visible'),
                'expected': check('#card', 'false', 'clipped'), 'symptom': check('#card', 'true', 'clipped')}
        self.assertEqual(len(self.validate(data).steps), 1)

    def test_final_assertion_is_a_contract_error_not_an_observation(self):
        self.data['steps'].append({'action': 'assert_text', 'target': '#result', 'value': 'empty'})
        with self.assertRaises(PlanValidationError) as error:
            self.validate(self.data)
        self.assertEqual(error.exception.code, 'PLAN_FINAL_ASSERTION')


if __name__ == '__main__':
    unittest.main()
