"""Deterministic Markdown report rendering; all model text is escaped as data."""

import html
import re


def escape(value):
    text = str(value).replace('\r', ' ').replace('\n', ' ')
    return html.escape(re.sub(r'([\\`*_{}\[\]()#+.!|>~-])', r'\\\1', text), quote=False)


def describe_check(check):
    target, value = check['selector'], check['value']
    if check['kind'] == 'text':
        return f'{target}의 화면 텍스트: “{value}”'
    if check['kind'] == 'count':
        return f'{target}에 해당하는 요소 {value}개'
    phrases = {'visible': ('표시됨', '표시되지 않음'), 'enabled': ('활성화됨', '비활성화됨'),
               'clipped': ('영역을 넘치거나 내용이 잘림', '영역 안에 표시됨'),
               'overlap': ('영역이 겹침', '영역이 겹치지 않음')}
    phrase = phrases[check['kind']][0 if value == 'true' else 1]
    return f"{target}{' 및 ' + check['other_selector'] if check['other_selector'] else ''}: {phrase}"


def render_report(report: dict) -> str:
    run, analysis = report.get('run'), report.get('analysis')
    environment = report['environment']
    env_text = f"{environment['width']} × {environment['height']}px · 앱 버전 {environment['app_version']}"
    if environment.get('test_context_id'):
        env_text += ' · 테스트 환경 ' + environment['test_context_id']
    lines = ['# QA 검증 보고서', '', '## ' + escape(report['title']), '',
             '- 작업 ID: ' + escape(report['job_id']), '- 상태: ' + escape(report['status']),
             '- 버그 유형: ' + escape(report['classification']),
             '- 환경: ' + escape(env_text), '- 사람의 검토: 필요']
    if run:
        lines += ['- 재현 판정: ' + escape(run['verdict']['value']),
                  '- 판정 코드: ' + escape(run['verdict']['reason_code'])]
    if report.get('error_code'):
        lines.append('- 처리 오류: ' + escape(report['error_code']))
    if report.get('regression'):
        lines.append('- 수정 후 비교: ' + escape(report['regression']))
    if report.get('intake'):
        item = report['intake']
        lines += ['', '## 제보 해석', '', '**기대 동작:** ' + escape(item['expected_behavior']),
                  '', '**기대 동작 출처:** ' + escape(item['expectation_source']),
                  '', '**신고된 증상:** ' + escape(item['symptom_description']), '', '**화면에서 확인한 내용:**', '']
        lines += ['- ' + escape(value) for value in item['observed_facts']]
    if report.get('questions'):
        lines += ['', '## 추가 확인 사항', ''] + ['- ' + escape(value) for value in report['questions']]
    if report.get('scenario'):
        lines += ['', '## 재현 순서', '']
        for i, step in enumerate(report['scenario']['steps'], 1):
            action = {'navigate': '페이지 이동', 'fill': '입력', 'click': '클릭', 'assert_text': '텍스트 확인'}[step['action']]
            lines.append(f'{i}. ' + escape(f"{action} · {step['target']}" + (f" · {step['value']}" if step['value'] else '')))
        for label, key in [('완료 확인', 'ready'), ('기대 결과', 'expected'), ('증상 확인', 'symptom')]:
            lines += ['', f'**{label}:** ' + escape(describe_check(report['scenario'][key]))]
    if run:
        lines += ['', '## 시도별 관찰', '']
        for item in run['evidence']:
            observed = {True: '관찰됨', False: '미관찰', None: '확인 불가'}
            lines += [f"- 시도 {item['attempt']}: " + escape(
                f"완료 {item['completed_steps']}단계 / 증상 {observed[item['symptom_observed']]} / "
                f"기대 결과 {observed[item['expected_observed']]} / 차단 {item['blocker'] or '없음'} / 근거 {item['id']}")]
    if analysis:
        lines += ['', '## 분석 및 해결 방향', '', escape(analysis['summary']), '',
                  '- 임시 영향도: ' + escape(analysis['severity']),
                  '- 영향도 근거: ' + escape(analysis['severity_reason']), '', '**원인 가설:**', '']
        lines += ['- ' + escape(item['description']) + ' (참조: ' + escape(', '.join(item['source_refs'])) + ')'
                  for item in analysis['hypotheses']]
        for item in analysis['solutions']:
            lines += ['', '### ' + escape(item['title']), '', escape(item['description']), '',
                      '근거: ' + escape(', '.join(item['source_refs'])), '', '**검증 절차:**', '']
            lines += ['- ' + escape(value) for value in item['verification_steps']]
            lines += ['', '**고려 사항:**', ''] + ['- ' + escape(value) for value in item['tradeoffs']]
        lines += ['', '**부족한 정보:**', ''] + ['- ' + escape(value) for value in analysis['missing_information']]
    lines += ['', '## 화면 근거', '']
    # Only engine-generated local names become links; no model-supplied URL is rendered.
    for snapshot in report['observations']:
        if snapshot['phase'] in {'discovery', 'blocked', 'verdict'}:
            name = snapshot['image']['name']
            if re.fullmatch(r'image_[a-f0-9]{64}\.png', name):
                lines += [f"계획 버전 {snapshot['revision']} / 시도 {snapshot['attempt']} / {escape(snapshot['phase'])} / 근거 {escape(snapshot['id'])}",
                          '', f'![검증 화면]({name})', '']
    lines += ['## 해석 시 주의', ''] + ['- ' + escape(value) for value in report['limitations']]
    return '\n'.join(lines) + '\n'
