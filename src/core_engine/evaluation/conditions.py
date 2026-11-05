"""Separate role/engine/delay development suite; does not change qa-small-v1 cases or grader."""

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
import time
from types import SimpleNamespace
from uuid import uuid4

from core_engine import (BrowserOptions, BrowserRunner, ConditionLimits, ConditionsService,
    GroqModel, JobStore, OpenAIModel, QARequest, Recorder, TestCondition)
from core_engine.qa_schema import decode_plan
from .condition_site import condition_site, response_plan
from .fixture_model import FixtureModel
from .runner import CallBudget, reset, source_metadata, write_json


SUITE_VERSION = 'qa-conditions-v1'


def registry(video=False):
    result = []
    for id_, role, engine, delay in (('admin', 'admin', 'chromium', 0), ('staff', 'staff', 'chromium', 0),
                                    ('staff_firefox', 'staff', 'firefox', 0), ('staff_webkit_slow', 'staff', 'webkit', 200)):
        result.append(TestCondition(id_, f'{role} / {engine} / {delay}ms', BrowserOptions(
            test_context_id=role, browser_name=engine, response_delay_ms=delay, record_video=video,
            session_headers={'Authorization': 'Bearer synthetic-' + role})))
    return tuple(result)


def request(origin):
    return QARequest('condition-report', 'demo', origin + '/app', None,
        '직원 계정에서 Preview report 버튼을 한 번 클릭하고 Complete 문구가 표시된 뒤 '
        'GET /app/api/preview 응답이 403이며 Unavailable 문구가 나타났습니다.',
        '같은 조건에서 GET /app/api/preview 응답은 200, 결과는 Preview ready여야 합니다.',
        '등록된 합성 테스트 계정이며 서버 데이터 변경 없이 조회하는 화면입니다.')


class ConditionOracle(Recorder):
    def __init__(self, *args):
        super().__init__(*args)
        self.truth = []

    async def capture(self, page, **kwargs):
        snapshot = await super().capture(page, **kwargs)
        if kwargs['phase'] in {'verdict', 'blocked'}:
            self.truth.append({'attempt': kwargs['attempt'], 'actions': await page.evaluate('window.fixtureActions || []'),
                'complete': await page.locator('#status').inner_text() == 'Complete',
                'result': await page.locator('#result').inner_text(),
                'statuses': [r['status'] for r in kwargs['network'] if r['url'].endswith('/app/api/preview')]})
        return snapshot


async def independently_check(child, condition, policy, store, expected_status):
    if not child['report'].get('scenario'):
        return {'passed': False, 'reason': 'NO_PLAN', 'job_id': None}
    plan = decode_plan(child['report']['scenario'], timeout_ms=condition.options.step_timeout_ms)
    job_id = store.create()
    recorder = ConditionOracle(store, job_id, condition.options)
    run = await BrowserRunner(policy, options=condition.options, recorder=recorder).run(
        plan, reset=reset, is_authorized=lambda: True)
    expected_text = 'Preview ready' if expected_status == 200 else 'Unavailable'
    passed = all(a.completed for a in run.attempts) and len(recorder.truth) == 2 and all(
        a['actions'] == ['preview'] and a['complete'] and a['result'] == expected_text
        and a['statuses'] == [expected_status] for a in recorder.truth)
    result = {'passed': passed, 'reason': 'PASS' if passed else 'INDEPENDENT_ACTION_OR_OUTCOME_MISMATCH',
              'job_id': job_id, 'attempts': recorder.truth, 'run': asdict(run)}
    store.write_json(job_id, 'oracle.json', result)
    return result


def grade_matrix(report, oracles, *, buggy):
    expected = {'admin': 'not_reproduced', **{key: 'reproduced' if buggy else 'not_reproduced'
        for key in ('staff', 'staff_firefox', 'staff_webkit_slow')}}
    by_id = {row['condition_id']: row for row in report['conditions']}
    row_grades = {key: key in by_id and by_id[key].get('verdict') == value and oracles.get(key, {}).get('passed') is True
                  for key, value in expected.items()}
    false_positives = sum(by_id.get(key, {}).get('verdict') == 'reproduced'
                         for key, value in expected.items() if value == 'not_reproduced')
    return {'passed': report['status'] == 'completed' and all(row_grades.values()), 'rows': row_grades,
        'normal_rows': sum(v == 'not_reproduced' for v in expected.values()),
        'false_positive_rows': false_positives,
        'normal_matrix_false_positive': (not buggy and any(row.get('verdict') == 'reproduced' for row in by_id.values()))}


async def run_suite(args, model=None, reviewer=None):
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    root = Path(args.output).resolve() / (stamp + '_conditions_' + uuid4().hex[:8])
    root.mkdir(parents=True, mode=0o700)
    store = JobStore(root / 'jobs')
    budget = CallBudget(args.max_model_calls)
    trials = []
    conditions = registry(args.video)
    manifest = {'suite': SUITE_VERSION, 'mode': 'live' if args.live else 'fixture', 'critic': args.critic,
        'source': source_metadata(), 'repeats': args.repeats, 'video': args.video,
        'condition_selection': 'host_fixed_registry_not_AI_selection',
        'case_ids': ['role_bug', 'role_normal'], 'planned_matrices': 2 * args.repeats,
        'conditions': [c.public() for c in conditions]}
    write_json(root / 'manifest.json', manifest)

    def persist():
        summary = {'suite': SUITE_VERSION, 'mode': manifest['mode'], 'planned_matrices': manifest['planned_matrices'],
            'finished_matrices': len(trials), 'passed_matrices': sum(t['grade']['passed'] for t in trials),
            'condition_rows': 4 * len(trials), 'passed_rows': sum(sum(t['grade']['rows'].values()) for t in trials),
            'normal_matrices': sum(not t['buggy'] for t in trials),
            'false_positive_matrices': sum(t['grade']['normal_matrix_false_positive'] for t in trials),
            'normal_rows': sum(t['grade']['normal_rows'] for t in trials),
            'false_positive_rows': sum(t['grade']['false_positive_rows'] for t in trials),
            'planned_fixed_checks': 3 * args.repeats,
            'passed_fixed_checks': sum(sum(r['passed'] for r in t['retests']) for t in trials),
            'provider_calls': len(budget.snapshot()), 'complete': len(trials) == manifest['planned_matrices'],
            'limitations': ['공개 합성 역할 사례 2개를 반복한 개발 세트입니다.',
                '브라우저/지연 조건은 실제 실행하지만 새로운 제품군의 일반화 평가가 아닙니다.',
                '조건 선택은 호스트 고정이며 AI의 조건 선택 정확도 점수가 아닙니다.']}
        write_json(root / 'results.json', trials)
        write_json(root / 'model_calls.json', budget.snapshot())
        write_json(root / 'summary.json', summary)
        return summary

    persist()
    for repeat in range(1, args.repeats + 1):
        for buggy in ((True, False) if repeat % 2 else (False, True)):
            trial_id = ('role_bug' if buggy else 'role_normal') + '/' + str(repeat)
            with condition_site(buggy=buggy) as (origin, policy, state, _):
                selected_model = model if args.live else FixtureModel(SimpleNamespace(category='bug', family='role',
                    expected_result=request(origin).expected_result, description=request(origin).description), origin + '/app',
                    reference_builder=lambda family, url: response_plan(url))
                service = ConditionsService(budget.wrap(selected_model, trial_id), policy, store, conditions,
                    reviewer=budget.wrap(reviewer, trial_id, 'critic') if reviewer else None,
                    limits=ConditionLimits(max_conditions=4, max_model_calls=24))
                async def reset_condition(*_):
                    return True
                started = time.monotonic()
                result = await service.run(request(origin), reset=reset_condition, is_authorized=lambda: True, select=False)
                elapsed = round(time.monotonic() - started, 3)
                oracles = {}
                for row in result.report['conditions']:
                    if row.get('job_id'):
                        condition = next(c for c in conditions if c.id == row['condition_id'])
                        child = store.read_json(row['job_id'], 'result.json')
                        expected_status = 403 if buggy and condition.id != 'admin' else 200
                        try:
                            oracles[condition.id] = await independently_check(child, condition, policy, store, expected_status)
                        except Exception as exc:
                            oracles[condition.id] = {'passed': False, 'reason': 'HARNESS_ERROR',
                                'error_type': type(exc).__name__, 'job_id': None}
                grade = grade_matrix(result.report, oracles, buggy=buggy)
                retests = []
                if buggy:
                    state['buggy'] = False
                    for id_ in result.report['matched_condition_ids']:
                        if budget.exhausted:
                            break
                        verified = await service.retest(result.job_id, id_, reset=reset_condition, is_authorized=lambda: True)
                        condition = next(c for c in conditions if c.id == id_)
                        try:
                            oracle = await independently_check(asdict(verified), condition, policy, store, 200)
                        except Exception as exc:
                            oracle = {'passed': False, 'reason': 'HARNESS_ERROR', 'error_type': type(exc).__name__, 'job_id': None}
                        retests.append({'condition_id': id_, 'job_id': verified.job_id, 'oracle': oracle,
                            'passed': verified.report['regression'] == 'resolved_in_test' and oracle['passed']})
                trials.append({'trial_id': trial_id, 'buggy': buggy, 'job_id': result.job_id, 'elapsed_seconds': elapsed,
                               'grade': grade, 'oracles': oracles, 'retests': retests})
                summary = persist()
                print(trial_id + ': ' + ('PASS' if grade['passed'] else 'FAIL'), flush=True)
                if budget.exhausted:
                    return root, summary
    return root, persist()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--critic', choices=('none', 'groq'), default='none')
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--max-model-calls', type=int, default=96)
    parser.add_argument('--video', action='store_true')
    parser.add_argument('--output', default='.qa-evals')
    args = parser.parse_args()
    if not 1 <= args.repeats <= 3 or not 1 <= args.max_model_calls <= 200 or (args.critic != 'none' and not args.live):
        parser.error('bounded repeats/calls and live mode for critic are required')
    root, summary = asyncio.run(run_suite(args, OpenAIModel.from_env() if args.live else None,
        GroqModel.from_env() if args.critic == 'groq' else None))
    print(root)
    raise SystemExit(0 if summary['complete'] and summary['passed_matrices'] == summary['planned_matrices']
                     and summary['passed_fixed_checks'] == summary['planned_fixed_checks'] else 1)


if __name__ == '__main__':
    main()
