import argparse
import contextlib
from dataclasses import replace
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

from core_engine import BrowserOptions, Generation, JobStore, ModelError, PolicyError, QARequest, QAService
from core_engine.evaluation.cases import CASES, reference_plan
from core_engine.evaluation.fixture_model import FixtureModel
from core_engine.evaluation.grading import grade, summarize
from core_engine.evaluation.runner import CallBudget, independently_replay, reset, run_suite
from core_engine.evaluation.site import fixture_site
from core_engine.evaluation import transfer
from core_engine.qa_schema import draft_plan, encode_plan


class EvaluationUnitTests(unittest.TestCase):
    def test_case_balance_and_paired_reports_are_identical(self):
        self.assertEqual(len(CASES), 12)
        self.assertEqual([sum(case.category == kind for case in CASES)
                          for kind in ('bug', 'normal', 'needs_info', 'environment')], [4, 4, 2, 2])
        for family in ('search', 'filter', 'clipping', 'overlap'):
            pair = [case for case in CASES if case.family == family and case.category in {'bug', 'normal'}]
            self.assertEqual(pair[0].report_text(), pair[1].report_text())

    def test_environment_does_not_pass_for_provider_error_or_question(self):
        case = next(case for case in CASES if case.variant == 'unavailable')
        requests = [{'status': 503}]
        for data in ({'status': 'failed', 'error_code': 'MODEL_AUTH_FAILED'},
                     {'status': 'needs_info', 'questions': ['환경을 알려주세요.']}):
            self.assertFalse(grade(case, data, {}, requests)['passed'])
        self.assertTrue(grade(case, {'status': 'inconclusive', 'error_code': 'HTTP_ERROR'}, {}, requests)['passed'])

    def test_usage_is_unknown_on_error_and_call_budget_is_enforced(self):
        class Unavailable:
            def generate(self, **kwargs):
                raise ModelError('MODEL_TIMEOUT')
        budget = CallBudget(1)
        wrapped = budget.wrap(Unavailable(), 'a')
        with self.assertRaises(ModelError) as first:
            wrapped.generate()
        self.assertEqual(first.exception.code, 'MODEL_TIMEOUT')
        with self.assertRaises(PolicyError) as second:
            wrapped.generate()
        self.assertEqual(second.exception.code, 'MODEL_CALL_LIMIT')
        self.assertEqual(len(budget.snapshot()), 1)
        self.assertIsNone(budget.snapshot()[0]['usage'])

    def test_abstentions_remain_in_denominator_and_partial_runs_are_marked(self):
        rows = [{'kind': 'initial', 'category': 'normal', 'status': 'needs_info', 'passed': False,
                 'verdict': None, 'elapsed_seconds': 1},
                {'kind': 'initial', 'category': 'bug', 'status': 'failed', 'passed': False,
                 'verdict': None, 'elapsed_seconds': 1},
                {'kind': 'initial', 'category': 'bug', 'status': 'not_run', 'passed': False,
                 'verdict': None, 'elapsed_seconds': 0}]
        result = summarize(rows, [{'usage': None, 'model': None}], mode='live')
        self.assertFalse(result['complete'])
        self.assertEqual(result['metrics']['false_positive']['denominator'], 1)
        self.assertEqual(result['metrics']['conclusive_coverage']['rate'], 0)
        self.assertEqual(result['metrics']['reproduction_success']['denominator'], 1)
        self.assertEqual(result['calls_without_usage'], 1)
        self.assertEqual(result['not_run'], 1)
        rows[-1]['status'] = 'interrupted'
        interrupted = summarize(rows, [], mode='live')
        self.assertFalse(interrupted['complete'])
        self.assertFalse(interrupted['valid_for_comparison'])


@unittest.skipUnless(importlib.util.find_spec('playwright'), 'install browser extra')
class EvaluationBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_transfer_cases_cover_enabled_text_visible_and_count(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(suite='transfer', case=None, repeats=1, seed=17, output=directory,
                                      max_model_calls=120, live=False, retest_fixed=True)
            with contextlib.redirect_stdout(io.StringIO()):
                root, summary, rows = await run_suite(args)
            self.assertEqual(len(rows), 12)
            self.assertTrue(all(row['passed'] for row in rows),
                            [(row['case_id'], row['reason'], row['error_code']) for row in rows if not row['passed']])
            self.assertEqual(json.loads((root / 'manifest.json').read_text())['suite_version'], 'qa-transfer-v1')
            self.assertEqual(summary['metrics']['fixed_pair_success']['numerator'], 4)
            for family in transfer.REPORTS:
                pair = [case.report_text() for case in transfer.CASES if case.family == family]
                self.assertEqual(pair[0], pair[1])

    async def test_all_fixtures_and_fixed_retests_with_real_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            args = argparse.Namespace(case=None, repeats=1, seed=17, output=directory,
                                      max_model_calls=120, live=False, retest_fixed=True)
            with contextlib.redirect_stdout(io.StringIO()):
                root, summary, rows = await run_suite(args)
            self.assertEqual(len(rows), 16)
            self.assertTrue(all(row['passed'] for row in rows), [(row['case_id'], row['kind'], row['reason'], row['error_code']) for row in rows if not row['passed']])
            self.assertTrue(summary['complete'])
            self.assertFalse(summary['is_ai_evaluation'])
            self.assertEqual(summary['metrics']['fixed_pair_success']['numerator'], 4)
            self.assertEqual(summary['metrics']['false_positive']['numerator'], 0)
            self.assertIn('AI 성능 점수 아님', (root / 'summary.md').read_text())
            self.assertEqual((root / 'results.json').stat().st_mode & 0o777, 0o600)
            self.assertTrue((root / 'results.csv').is_file())
            for row in rows:
                if row.get('report_path'):
                    self.assertTrue((root / row['report_path']).is_file())
                if row.get('oracle_path'):
                    self.assertTrue((root / row['oracle_path']).is_file())

    async def test_independent_oracle_rejects_right_label_without_triggering_reported_bug(self):
        case = next(case for case in CASES if case.id == 'search_bug')
        with tempfile.TemporaryDirectory() as directory, fixture_site(case.family) as site:
            store, options = JobStore(directory), BrowserOptions()
            reference = reference_plan(case.family, site.url)
            wrong = replace(reference, steps=reference.steps[:3])  # Searches once; never performs the missing query.

            class WrongPlan(FixtureModel):
                def generate(self, **kwargs):
                    if 'plan' in kwargs['schema']['properties']:
                        return Generation(json.dumps({'status': 'ready', 'questions': [], 'rationale': '잘못된 계획',
                                                      'plan': draft_plan(encode_plan(wrong))}), 'offline-fixture')
                    return super().generate(**kwargs)

            service = QAService(WrongPlan(case, site.url), site.policy(), store, options=options)
            request = QARequest('report', 'qa-evaluation', site.url, description=case.description,
                                expected_result=case.expected_result, test_data=case.test_data)
            from dataclasses import asdict
            data = asdict(await service.run(request, reset=reset, is_authorized=lambda: True))
            self.assertEqual(data['report']['run']['verdict']['value'], 'reproduced')
            oracle = await independently_replay(case, data, site, store, options)
            self.assertEqual(grade(case, data, oracle, site.requests)['reason'], 'INDEPENDENT_ORACLE_FAILED')

    async def test_fixture_urls_and_model_context_do_not_expose_labels(self):
        from core_engine.evaluation.runner import capture_source
        pair = [case for case in CASES if case.family == 'search' and case.category in {'bug', 'normal'}]
        image_hashes = []
        for case in pair:
            with tempfile.TemporaryDirectory() as directory, fixture_site(case.family) as site:
                store, options = JobStore(directory), BrowserOptions()
                image = await capture_source(case, site, store, options)
                image_hashes.append(image.sha256)
                site.activate(case.variant)
                calls = []

                class InspectingModel(FixtureModel):
                    def generate(self, **kwargs):
                        calls.append(kwargs['context'])
                        return super().generate(**kwargs)

                request = QARequest('opaque', 'qa-evaluation', site.url, image, **case.report_text())
                await QAService(InspectingModel(case, site.url), site.policy(), store, options=options).run(
                    request, reset=reset, is_authorized=lambda: True)
                encoded = json.dumps(calls, ensure_ascii=False)
                for forbidden in (case.id, '"category"', '"variant"', '"ground_truth"', '"oracle"', '"reference_plan"'):
                    self.assertNotIn(forbidden, encoded)
                self.assertNotIn('bug', site.url)
                self.assertNotIn('control', site.url)
                self.assertNotIn('버그', site.respond(site.page_path)[2].decode())
        self.assertEqual(image_hashes[0], image_hashes[1])


if __name__ == '__main__':
    unittest.main()
