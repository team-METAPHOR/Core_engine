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
    if check['kind'] == 'response_status':
        return f"{check['other_selector']} {target} 응답 코드: {value}"
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
    if environment.get('browser_name'):
        env_text += ' · ' + environment['browser_name'] + ' ' + (environment.get('browser_version') or '버전 미확인')
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
    if report.get('diagnosis'):
        diagnosis = report['diagnosis']
        lines += ['', '## 재현 결과와 중단 이유', '', escape(diagnosis['summary']),
                  '', '코드 수준의 근본 원인이 확정된 것은 아닙니다.', '', '**다음 조치:**', '']
        lines += ['- ' + escape(item['message']) + ' (담당: ' + escape(item['owner']) + ')'
                  for item in diagnosis['next_actions']]
        for item in diagnosis['attempts']:
            lines += ['- ' + escape(f"시도 {item['attempt']} · {item['stage']} · 완료 {item['completed_steps']}/{item['total_steps']} · 중단 {item['blocker'] or '없음'}")]
    if report.get('intake'):
        item = report['intake']
        lines += ['', '## 제보 해석', '', '**기대 동작:** ' + escape(item['expected_behavior']),
                  '', '**기대 동작 출처:** ' + escape(item['expectation_source']),
                  '', '**신고된 증상:** ' + escape(item['symptom_description']), '', '**화면에서 확인한 내용:**', '']
        lines += ['- ' + escape(value) for value in item['observed_facts']]
    if report.get('questions'):
        lines += ['', '## 추가 확인 사항', ''] + ['- ' + escape(value) for value in report['questions']]
    if report.get('plan_validation'):
        lines += ['', '## 실행 전 계획 검증', '']
        lines += [f"- 계획 제안 {item['proposal'] + 1}: " + escape(item['code']) for item in report['plan_validation']]
    if report.get('plan_review', {}).get('enabled'):
        item = report['plan_review']
        lines += ['', '## 실행 전 비판 모델 검토', '', '- 상태: ' + escape(item['status'])]
        if item.get('review'):
            lines += ['', escape(item['review']['summary'])]
            lines += ['- ' + escape(issue['code'] + ': ' + issue['reason']) for issue in item['review']['issues']]
        if item['status'] == 'revised_unreviewed':
            lines += ['', '지적 후 수정된 계획은 호스트 검증을 통과했습니다. 비판 모델의 재검토는 수행하지 않았습니다.']
        if item.get('reviewed_plan_sha256') and item.get('final_plan_sha256') != item['reviewed_plan_sha256']:
            lines += ['', '검토한 초기 계획과 최종 실행 계획의 해시가 다릅니다. 초기 검토를 최종 계획의 승인으로 해석하지 마세요.']
        lines += ['', '비판 모델의 동의는 버그 판정이 아닙니다. 판정은 브라우저 관찰 근거를 따릅니다.']
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
    if report.get('video_enabled'):
        lines += ['', '## 재현 영상', '', '민감 요소를 가린 화면 샘플링 영상입니다. 오디오는 없으며 모든 순간의 동작을 기록하는 원본 영상은 아닙니다.', '']
        if not report.get('videos'):
            lines += ['브라우저 영상이 생성되지 않았습니다. 초기 실행 오류와 추가 확인 사항을 확인하세요.']
        for item in report.get('videos', []):
            artifact = item.get('artifact') or {}
            name = artifact.get('name', '')
            if item['status'] == 'ready' and re.fullmatch(r'video_[a-f0-9]{64}\.webm', name):
                lines += [f"- 계획 버전 {item['revision']} · 시도 {item['attempt']} · [영상 보기]({name}) · {item['duration_seconds']}초 · 잘림: {item['truncated']}"]
            else:
                lines += ['- ' + escape(f"시도 {item['attempt']} · {item['status']} · {item.get('capture_error') or '없음'}")]
    exported = report.get('test_export', {})
    if exported.get('status') == 'ready' and exported.get('artifact', {}).get('name') == 'reproduction.spec.ts':
        lines += ['', '## 실행 가능한 재현 테스트', '', '[Playwright 테스트 다운로드](reproduction.spec.ts)', '',
                  '정상 동작을 검사하므로 버그 버전에서는 실패하고 수정 버전에서는 통과해야 합니다. 실행할 테스트 환경과 초기화 모듈을 설정하세요.']
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
