import copy
import unittest

from core_engine.analysis_schema import validate_output
from core_engine.planning import PlanValidationError, validate_candidate
from core_engine.qa_schema import PLAN_SCHEMA, draft_plan
from core_engine.scenario_contract import ground_contract, planning_schema
from qa_fixtures import intake, plan


class ScenarioContractTests(unittest.TestCase):
    def setUp(self):
        self.url = 'https://example.test/products'
        self.report = {'description': 'cat 입력 후 검색 클릭. missing 입력 후 검색 클릭. done 표시 후 확인.',
                       'expected_result': 'empty 표시', 'test_data': ''}
        self.intake = intake()
        self.intake.update(required_actions=[
            {'action': 'fill', 'value': 'cat', 'source_quote': 'cat 입력'},
            {'action': 'click', 'value': '', 'source_quote': '검색 클릭'},
            {'action': 'fill', 'value': 'missing', 'source_quote': 'missing 입력'},
            {'action': 'click', 'value': '', 'source_quote': '검색 클릭'}], actions_complete=True,
            completion_signal={'value': 'done', 'source_quote': 'done 표시 후 확인'})
        self.contract = ground_contract(self.intake, self.report)

    def validate(self, data):
        return validate_candidate(data, self.report, entry_url=self.url, timeout_ms=3000, contract=self.contract)

    def test_complete_sequence_accepts_setup_assertions_but_preserves_trigger(self):
        data = plan(self.url)['plan']
        self.validate(data)
        data['steps'][-1] = {'action': 'navigate', 'target': self.url, 'value': ''}
        with self.assertRaises(PlanValidationError) as caught:
            self.validate(data)
        self.assertEqual(caught.exception.code, 'PLAN_UNSUPPORTED_NAVIGATION')

    def test_critic_cannot_replace_reported_completion_or_remove_click(self):
        data = plan(self.url)['plan']
        data['ready']['value'] = 'ready'
        with self.assertRaises(PlanValidationError) as caught:
            self.validate(data)
        self.assertEqual(caught.exception.code, 'PLAN_REPORTED_COMPLETION_CHANGED')
        data = plan(self.url)['plan']
        data['steps'].pop()
        with self.assertRaises(PlanValidationError) as caught:
            self.validate(data)
        self.assertEqual(caught.exception.code, 'PLAN_REPORTED_SEQUENCE_CHANGED')

    def test_quote_and_literal_must_come_from_report(self):
        for key, value in [('source_quote', 'invented completion'), ('value', 'not in quote')]:
            altered = copy.deepcopy(self.intake)
            altered['completion_signal'][key] = value
            with self.assertRaises(ValueError):
                ground_contract(altered, self.report)

    def test_provider_schema_constrains_ready_and_final_action_without_mutating_base(self):
        base = copy.deepcopy(PLAN_SCHEMA)
        schema = planning_schema(PLAN_SCHEMA, self.contract)
        draft = plan(self.url)
        draft['plan'] = draft_plan(draft['plan'])
        validate_output(draft, schema)
        draft['plan']['ready']['value'] = 'ready'
        with self.assertRaises(ValueError):
            validate_output(draft, schema)
        self.assertEqual(PLAN_SCHEMA, base)

    def test_observation_cannot_be_grounded_as_navigation_without_destination(self):
        altered = copy.deepcopy(self.intake)
        altered['required_actions'].append({'action': 'navigate', 'value': '', 'source_quote': 'done 표시 후 확인'})
        with self.assertRaises(ValueError):
            ground_contract(altered, self.report)
        altered['required_actions'][-1]['value'] = 'done'
        with self.assertRaises(ValueError):
            ground_contract(altered, self.report)

    def test_explicit_later_navigation_preserves_quoted_destination(self):
        self.report['description'] += ' 이후 /app/details로 이동합니다.'
        self.intake['required_actions'].append({'action': 'navigate', 'value': '/app/details',
                                                'source_quote': '/app/details로 이동합니다.'})
        self.contract = ground_contract(self.intake, self.report)
        data = plan(self.url)['plan']
        data['steps'].append({'action': 'navigate', 'target': 'https://example.test/app/details', 'value': ''})
        self.validate(data)
        data['steps'][-1]['target'] = 'https://example.test/app/elsewhere'
        with self.assertRaises(PlanValidationError):
            self.validate(data)

    def test_observing_a_result_never_adds_a_second_click(self):
        self.intake['required_actions'].append({'action': 'observe', 'value': '', 'source_quote': 'done 표시 후 확인'})
        self.contract = ground_contract(self.intake, self.report)
        self.assertEqual(len(self.contract['required_actions']), 4)
        self.assertEqual(self.contract['reported_observations'], ['done 표시 후 확인'])
        self.validate(plan(self.url)['plan'])
