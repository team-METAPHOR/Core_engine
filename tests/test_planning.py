import copy
import unittest

from core_engine.analysis_schema import validate_output
from core_engine.planning import PlanValidationError, validate_candidate
from core_engine.qa_schema import PLAN_DATA_SCHEMA, PLAN_SCHEMA, draft_plan, flatten_draft
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

    def test_whole_page_exact_text_is_rejected_before_browser_execution(self):
        for key in ('ready', 'expected', 'symptom'):
            data = copy.deepcopy(self.data)
            data[key] = check('body', 'short status')
            with self.assertRaises(PlanValidationError) as error:
                self.validate(data)
            self.assertEqual(error.exception.code, 'PLAN_OVERBROAD_TEXT_TARGET')
        data = copy.deepcopy(self.data)
        data['steps'].insert(1, {'action': 'assert_text', 'target': 'body', 'value': 'short status'})
        with self.assertRaises(PlanValidationError) as error:
            self.validate(data)
        self.assertEqual(error.exception.code, 'PLAN_OVERBROAD_TEXT_TARGET')

    def test_new_actions_and_checks_round_trip_in_model_schema(self):
        from core_engine.qa_schema import encode_plan
        for action in ('reload', 'back'):
            data = copy.deepcopy(self.data)
            data['steps'][-1] = {'action': action, 'target': '', 'value': ''}
            data['expected'] = check('#choice', 'true', 'attribute', 'aria-pressed')
            data['symptom'] = check('#choice', 'false', 'attribute', 'aria-pressed')
            data['ready'] = check('', '/products', 'url_path')
            validate_output({'status': 'ready', 'questions': [], 'rationale': 'test', 'plan': draft_plan(data)}, PLAN_SCHEMA)
            self.assertEqual(encode_plan(self.validate(data)), data)

    def test_generated_draft_separates_setup_from_the_final_trigger(self):
        data = draft_plan(self.data)
        generated = {'status': 'ready', 'questions': [], 'rationale': 'test', 'plan': data}
        validate_output(generated, PLAN_SCHEMA)
        self.assertEqual(flatten_draft(data), self.data)
        data['final_action'] = {'action': 'assert_text', 'target': '#ready', 'value': 'done'}
        with self.assertRaises(ValueError):
            validate_output(generated, PLAN_SCHEMA)
        data['final_action'] = {'action': 'fill', 'target': '#query', 'value': 'missing'}
        data['setup_steps'] = self.data['steps'][:1]
        validate_output(generated, PLAN_SCHEMA)
        self.assertEqual([s['action'] for s in flatten_draft(data)['steps']], ['navigate', 'fill'])
        data['setup_steps'] = []
        compiled = flatten_draft(data, entry_url='https://example.test/products')
        self.assertEqual([s['action'] for s in compiled['steps']], ['navigate', 'fill'])
        self.assertEqual(compiled['steps'][0]['target'], 'https://example.test/products')
        data['final_action'] = {'action': 'click', 'target': '#search', 'value': 'invented value'}
        with self.assertRaises(ValueError):
            validate_output(generated, PLAN_SCHEMA)


if __name__ == '__main__':
    unittest.main()
