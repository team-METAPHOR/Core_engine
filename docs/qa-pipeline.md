# 스크린샷 기반 QA 엔진 연동 명세 v2

## 책임과 호출 단위

백엔드는 고객·프로젝트 권한, 등록된 웹앱 URL, 외부 AI 전송 허용, 합성 테스트 데이터, 작업 큐와 결과 접근 권한을 관리합니다. 엔진은 제보 해석부터 실행·보고서 저장까지 담당합니다. 프론트는 업로드·설명 입력, 추가 질문 답변, 진행 상태와 근거, 수정 후 재검증 요청을 표시합니다. 엔진에는 HTTP 서버나 관제 페이지가 포함되지 않습니다.

`QAService.run()`은 비동기 함수입니다. 하나의 작업에는 한 워커만 배정합니다. 별도 워커에서 실행하고, 브라우저·모델의 진행 중 요청을 강제 종료해야 하는 운영 환경에는 프로세스 감독을 추가합니다.

## 입력 객체

```python
QARequest(
    report_id="report-123",
    program_id="project-1",
    entry_url="https://staging.example.com/search",
    screenshot=Screenshot(image_bytes),
    description="선택 설명",
    expected_result="선택: 기대 동작",
    test_data="선택: 승인된 합성 테스트 데이터 설명",
)
```

| 필드 | 조건 |
|---|---|
| `report_id`, `program_id` | 최대 128자, 영문·숫자 시작, 영문·숫자·`_ . : -` 허용 |
| `entry_url` | 프로젝트 범위 안의 HTTP(S) URL, 최대 2048자 |
| `screenshot` | 선택적 `Screenshot` 객체; 이미지와 설명 중 최소 하나 필요 |
| `description` | 선택 설명, 최대 8000자 |
| `expected_result` | 선택 기대 동작, 최대 2000자 |
| `test_data` | 선택 합성 데이터 설명, 최대 2000자 |

스크린샷만으로도 접수할 수 있습니다. 정적인 이미지에 없는 시간 순서·서버 응답·사용자 의도는 확인된 사실로 취급하지 않습니다. 기대 동작이나 재현 조건이 불충분하면 `needs_info`로 추가 질문을 반환합니다. 명확한 시각적 문제 후보는 `expectation_source=image_inferred`로 진행할 수 있지만 최종 검토가 필요합니다.

업로드 이미지는 정적 PNG/JPEG/WebP만 허용합니다. 파일 4 MiB, 각 변 4096px, 총 800만 픽셀 상한을 적용하고 PNG로 다시 인코딩해 메타데이터를 제거합니다. 좌표를 알고 있는 비밀 영역은 `Screenshot(image_bytes).masked(((x1,y1,x2,y2), ...))`로 가립니다. 이 좌표는 EXIF 방향 보정 후 이미지 기준입니다. 이미지 안의 개인정보 자동 탐지 기능은 아닙니다.

## 신뢰된 실행 설정

`RunPolicy`는 백엔드가 승인한 원점·경로·DNS/IP·메서드 범위입니다. 사용자나 모델의 허용 범위 값을 그대로 채택하지 않습니다. `BrowserOptions`는 320~1920px 너비, 320~1080px 높이, 앱 버전, 추가 마스킹 선택자와 선택적 고정 테스트 세션 헤더를 받습니다.

```python
options = BrowserOptions(
    width=1280, height=800, app_version="build-123", test_context_id="synthetic-user-a", step_timeout_ms=3000,
    mask_selectors=(".customer-name",),
    session_headers={"Cookie": "synthetic-test-session=..."},
)
```

세션 헤더는 신뢰된 서버 비밀정보입니다. `Authorization`과 `Cookie`만 지원하고 모델·보고서·공개 환경 설정에 포함하지 않습니다. 원점·경로 검사를 통과한 HTTPS 전송에만 붙습니다. `0.4.1`부터 HTTP+인증 설정은 생성자에서 `CREDENTIALS_REQUIRE_HTTPS` 예외로 거절하고 요청 직전에도 재검사합니다. 익명 HTTP는 허용합니다. 고정 헤더 모드에서는 세션 자동 갱신·Set-Cookie 반영을 지원하지 않습니다. 계정으로 로그인하려면 [등록 계정 설정](registered-test-login.md)을 사용하며 같은 HTTPS 제약이 적용됩니다. 백엔드는 설정 저장/변경과 워커 실행 전에 이 조건을 검증해야 합니다.

`reset(attempt)`는 초기화 성공 시 정확히 `True`를 반환하는 비동기 함수입니다. 첫 관찰 전 `0`, 두 검증 시도 전 `1`, `2`로 호출됩니다. 선택자 보정 시 전체 두 시도를 다시 수행하므로 콜백은 반복 호출을 지원해야 합니다. 실패하면 테스트를 보류합니다.

`is_authorized()`는 현재 프로젝트 승인·중지 상태와 외부 AI 처리 허용을 확인하는 동기 함수입니다. 모델 호출 전후와 브라우저 요청·동작에서 확인합니다. `threading.Event`를 `cancelled`로 넘기면 실행 중 취소할 수 있습니다. 이미 보낸 요청과 진행 중인 유료 생성은 회수할 수 없습니다.

## 단계별 계약

1. **해석:** 원본 제보 이미지와 정제된 설명을 모델에 전달합니다. `observed_facts`, `hypotheses`, `expected_behavior`, `expectation_source`, `symptom_description`, `preconditions`, `questions`를 구분합니다.
2. **페이지 관찰:** 승인된 URL을 새 브라우저에서 열어 최대 80개 요소와 마스킹된 화면을 수집합니다. 요소는 CSS 선택자·태그·이름·짧은 텍스트·활성화 상태, `bounds`, `client_size`, `scroll_size`, 제한된 `state_attributes`를 포함합니다. 위치와 크기는 실제 현재 화면의 측정값이며 평가용 정답이 아닙니다. 전체 DOM과 입력값은 보내지 않습니다.
3. **계획:** 현재 페이지와 원본 제보 이미지(있는 경우)를 구분해 모델에 전달합니다. 모델은 최대 20개 `navigate/reload/back/click/fill/assert_text`와 `ready/expected/symptom` 조건을 생성합니다. 첫 URL은 백엔드에 등록된 입력으로 고정합니다. 다른 URL도 매번 범위를 검사합니다.
4. **실행:** 동작 전후 화면을 저장합니다. 중간 완료는 `assert_text`, 최종 완료는 `ready`로 확인합니다. 새 컨텍스트·서버 초기화로 같은 계획을 두 번 실행합니다.
5. **보정:** `STEP_OR_READINESS_FAILED`가 발생한 실제 조작 단계에 한해, 실패 시점 화면에 존재하는 선택자로 바꿀 수 있습니다. 동작·입력값·순서·최종 조건은 변경 불가입니다. 보정 후 이전 실행을 확정 근거로 합치지 않고 두 시도를 다시 실행합니다.
6. **판정·보고:** 엔진이 판정한 결과를 모델에 전달해 원인 가설·해결 방향을 작성합니다. 모델 요약에 실패해도 이미 얻은 실행 결과는 보존됩니다. 코드 수정·패치·PR·개발자 알림은 수행하지 않습니다.

단계별 모델 입출력 스키마는 `qa_schema.py`에 정의되어 있으며 공급자 Structured Outputs 이후에도 다시 검증합니다. 재현 계획 생성의 문장 의미가 제품 요구사항과 맞는지까지 형식 검증만으로 보장할 수는 없습니다.

### 실행 전 계획 검증과 재작성

모델 작성 형식은 `setup_steps`와 `final_action`을 분리합니다. 준비 단계는 최대 18개이며, 마지막 조작은 `navigate/reload/back/click/fill` 중 하나여야 합니다. 정적 관찰은 준비 단계 없이 마지막 조작을 페이지 이동으로, 입력 후 상태 검사는 마지막 조작을 입력으로 표현할 수 있습니다. 마지막 검사 단계를 없애기 위해 클릭을 덧붙이는 오류를 줄이기 위한 구조입니다. 엔진이 최초 등록 URL 이동을 보장하며 모델이 빠뜨린 경우 앞에 추가합니다. 따라서 새 브라우저에서 준비 단계가 빈 페이지에 실행되지 않습니다. 모델 응답 검증 후 기존 `steps` 배열로 합치므로 저장된 시나리오·백엔드·재검증 계약은 유지됩니다. 임의의 동작·검사를 자동 삭제하지 않습니다.

참·거짓 검사는 JSON Schema에서 `true/false` 문자열만 허용하고, 두 요소 검사는 두 번째 선택자를 필수로 요구합니다. `ready`가 `expected` 또는 `symptom`과 같거나, 마지막 단계에서 결과를 강제하거나, 제보에 없는 입력을 생성하면 실행 전에 거절합니다. 비어 있지 않은 `fill` 값은 정제된 `description/expected_result/test_data` 중 하나에 원문 그대로 포함되어야 합니다. 이 검사는 입력의 출처를 확인할 뿐, 입력의 의미나 사용 순서가 옳다는 것을 증명하지는 않습니다. 필요한 입력이 없으면 추가 질문으로 넘겨야 합니다.

거절 시 오류 코드와 문제 필드 경로를 모델에 전달하여 기본 1회 재작성을 허용합니다. 이 단계는 재현 실행 이전이며 브라우저 판정이나 평가 정답을 피드백하지 않습니다. 유효한 계획을 확정한 뒤의 재현 실행과 수정 후 재검증에서는 기존처럼 입력·순서·판정 조건을 고정합니다. 범위 위반이나 비밀 값은 재작성으로 우회하지 않습니다.

`report.plan_validation`은 `{proposal, code, field}` 배열입니다. `proposal`은 0부터 시작합니다. 코드는 `PLAN_INVALID_CONTRACT`, `PLAN_FINAL_ASSERTION`, `PLAN_READINESS_IS_OUTCOME`, `PLAN_UNSUPPORTED_INPUT` 등입니다. 0.7.0은 전체 body/html/*의 정확 텍스트 비교를 거절하는 `PLAN_OVERBROAD_TEXT_TARGET`도 추가합니다. 최종 거절의 상위 오류 코드는 기존 `INVALID_PLAN_OR_BASELINE`을 유지합니다. 모델 호출 기록에는 `plan_revision`, 진행 이벤트에는 `plan_rejected`가 추가됩니다. 기존 `schema_version=2`의 선택적 필드 추가이므로 소비자는 알 수 없는 필드·이벤트를 허용해야 합니다.

한글 등 비 ASCII 제보는 모델이 읽는 텍스트에서 원문을 유지합니다. 잘림은 원문 문자열이 DOM에 남아 있을 수 있으므로 `text` 비교가 아니라 `clipped` 검사로 검증해야 합니다. 초기 화면 상태와 최종 작업 완료 신호도 구분합니다.

### 선택적 비판 모델

`QAService(..., reviewer=GroqModel.from_env())`로 실행 전 계획 검토를 활성화합니다. 기본값 `None`은 비활성입니다. 정제된 제보·현재 요소·초기 계획·검증 오류만 텍스트로 전달하고 이미지는 보내지 않습니다. 호스트 검증과 비판자 피드백은 같은 재작성 상한을 공유하며 비판 호출은 1회입니다. 검토의 동의로 호스트 검증이나 실제 브라우저 판정을 대체하지 않습니다.

`report.plan_review`에 활성화·진행 상태·검토 응답·검토한 계획과 최종 계획 해시를 기록합니다. `revised_unreviewed`는 수정본의 호스트 검증만 수행했다는 뜻이며 비판자의 최종 승인으로 표시하면 안 됩니다. 새 이벤트는 `reviewing_plan`, `plan_reviewed`, 새 호출 단계는 `plan_review`입니다. `model_calls.role`은 `primary/critic`이며 모두 기존 호출·시간·토큰 예산을 공유합니다. 인증·한도·잘못된 응답은 실행 전에 실패로 반환하며 암묵적인 공급자 대체는 없습니다. `retest()`에서는 비판자를 건너뜁니다. 전체 설정·필드·오류 정책은 [비판 모델 연동 명세](critic-review-proposal.md)를 참고하세요.

## 검증 조건

모든 조건은 `kind`, `selector`, `value`, `other_selector`를 갖습니다. `value`는 문자열입니다.

| kind | value와 동작 |
|---|---|
| `text` | 요소의 화면 텍스트를 양끝 공백 제거 후 정확히 비교 |
| `visible` | `true/false`; 0개 요소는 보이지 않음, 2개 이상은 모호하여 보류 |
| `enabled` | `true/false`; 단일 표시 요소의 활성화 상태 |
| `count` | `0`~`1000`; 선택자에 맞는 요소 수 |
| `overlap` | `true/false`; 두 표시 요소의 사각 영역이 양축으로 1px 초과 교차하는지 검사. `other_selector` 필수 |
| `clipped` | `true/false`; 화면 경계 초과 또는 요소 스크롤 크기가 내부 크기를 1px 초과하는지 검사 |

0.7.0의 `url_path/attribute` 검사와 새로운 동작·오류 필드는 [확장 계약](qa-actions-and-diagnostics.md)을 참고하세요. `other_selector`는 overlap의 두 번째 선택자, response_status의 메서드, attribute의 허용된 ARIA 속성 이름이며 나머지는 빈 문자열입니다. text/enabled/overlap/clipped의 대상이 없거나 여러 개면 판단을 보류합니다. 이 검사는 임의 픽셀 비교·드래그·iframe 내부 탐색·소스 코드 검사·성능 벤치마크를 수행하지 않습니다. 겹침과 넘침이 의도된 디자인인지 판단할 기준은 제보·제품 요구사항에서 제공해야 합니다.

완료 조건 확인 뒤 `symptom=true, expected=false`일 때 증상 관찰, 그 반대는 정상 결과 관찰입니다. 둘 다 참이거나 둘 다 거짓이면 `ORACLE_AMBIGUOUS`로 보류합니다. 두 시도의 관찰이 일치해야 `reproduced` 또는 `not_reproduced`가 됩니다.

AI 계획의 마지막 동작은 `navigate/reload/back/fill/click`이어야 합니다. 마지막에 정상 결과를 `assert_text`로 강제하면 버그 화면에서는 판정 단계에 도달하지 못하므로 이러한 계획은 실행 전에 거절합니다. 최종 완료 신호는 `ready`, 정상·버그 결과는 `expected/symptom`에만 넣습니다. 중간 상태를 확인하는 `assert_text`는 계속 지원합니다.

## 결과·이벤트와 화면 연결

`dataclasses.asdict(QAResult)`를 JSON으로 직렬화합니다.

| 필드 | 의미 |
|---|---|
| `job_id` | 엔진 생성 32자리 작업 ID |
| `report_id`, `program_id` | 입력 제보·프로젝트 ID |
| `status` | `needs_info/completed/inconclusive/cancelled/failed` |
| `questions` | 추가 답변이 필요한 질문 목록 |
| `error_code` | 처리 오류 코드 또는 null |
| `report` | 아래 상세 QA 보고서 |

보고서에는 `schema_version=2`, 제목·일반 버그 유형, 항상 참인 `human_review_required`, 공개 실행 환경, 제보 해석, 시나리오, 계획 해시, 시도별 근거와 판정(`run`), AI 분석(`analysis`), 관찰 기록, 계획 보정 기록, 모델 호출·토큰 사용량, 이전 작업과 재검증 비교가 들어갑니다.

`status=completed`는 처리가 끝났다는 뜻입니다. 버그가 없거나 수정됐다는 뜻이 아닙니다. 실제 판정은 `report.run.verdict.value`를 표시합니다. 보고서 작성용 AI만 실패한 경우 `completed`와 `error_code`가 함께 올 수 있으며 `analysis=null`입니다.

`JobStore.read_json(job_id, "events.json")`에서 순서가 있는 이벤트를 읽습니다. 주요 단계는 `received`, `interpreting`, `discovering`, `planning`, `running`, `replanning`, `reporting`, `retesting`과 최종 상태입니다. `snapshots.json`에는 단계별 최신 관찰이 원자적으로 갱신됩니다. 폴링/SSE/WebSocket 전달은 백엔드에서 연결합니다.

프론트에서는 모델·페이지 텍스트를 신뢰된 HTML로 삽입하지 않습니다. 엔진 Markdown은 텍스트를 이스케이프하고 엔진이 만든 로컬 PNG 이름만 이미지 링크로 사용합니다. JSON 기반 화면도 텍스트 렌더링과 이미지 접근 권한 검사가 필요합니다.

## 추가 답변과 수정 후 재검증

```python
continued = await service.resume(
    previous_job_id, "추가 답변", reset=reset_test_state, is_authorized=check_authorization,
)
verified = await service.retest(
    baseline_job_id, reset=reset_test_state, is_authorized=check_authorization,
)
```

`resume`은 `needs_info` 작업에만 적용하고 새 작업을 생성해 `parent_job_id`로 연결합니다. 원본 제보 이미지와 기존 설명에 답변을 추가하여 처음부터 해석합니다. 실제 기대 동작이 바뀌는 추가 답변은 새 계획에 반영될 수 있습니다.

`retest`는 저장된 계획·입력값·판정 조건을 그대로 사용하며 자동 선택자 보정을 하지 않습니다. 저장한 계획 해시를 확인하고 현재 승인 범위에 다시 검증합니다. 앱 버전만 바뀌었다면 새 `BrowserOptions.app_version`을 사용한 서비스를 생성할 수 있습니다. 화면 크기·테스트 컨텍스트 ID·세션 모드가 바뀌면 `BASELINE_ENVIRONMENT_MISMATCH`로 실행을 보류합니다. 세션 헤더를 사용하는 경우 합성 계정·데이터 환경을 나타내는 `test_context_id`를 명시해야 하며 실제 같은 환경인지 보장하는 책임은 백엔드에 있습니다.

| regression | 의미 |
|---|---|
| `resolved_in_test` | 이전 재현됨 → 이번 재현 안 됨 |
| `still_reproduced` | 이전과 이번 모두 재현됨 |
| `newly_reproduced` | 이전 미재현 → 이번 재현됨 |
| `unchanged` | 이전과 이번 모두 미재현 |
| `inconclusive` | 이번 판정 불가 |
| `no_confirmed_baseline` | 비교할 확정된 이전 결과 부족 |

작업 저장소는 권한 부여 수단이 아닙니다. 작업 조회·재개·재검증 전에 백엔드가 고객과 제보 권한을 확인해야 합니다. 엔진은 프로그램 ID 혼용과 저장된 제보 ID 불일치를 추가로 거절합니다.

## 저장 파일

- `request.json`: 정제된 설명, 제보 식별자·등록 URL, 입력 이미지 참조.
- `scenario.json`: 마지막 실행 계획·고정 판정 조건·해시·초기 해석.
- `run_0.json`, `run_1.json`, `run_2.json`: 보정 버전별 두 시도의 결과.
- `snapshots.json`: 단계·계획 버전·해시·관찰 ID·이미지 참조·정제된 요소·요청·오류 기록.
- `image_<sha256>.png`: 정규화한 이미지. 읽을 때 해시를 재확인.
- `events.json`: 순서와 시각이 있는 단계 이벤트.
- `result.json`, `report.md`: 최종 결과와 한국어 보고서.

디렉터리는 0700, 파일은 0600으로 생성하며 경로 이동·심볼릭 링크를 거절합니다. 한 파일은 최대 8 MiB, 작업 화면은 최대 256개·총 64 MiB입니다. 다중 워커 동시 쓰기는 지원하지 않습니다. 삭제·보존 기간·저장소 암호화·백업은 배포 백엔드에서 담당합니다.

## 한도와 실패

`QALimits` 기본값: 모델 호출 6회, 실행 전 계획 재작성 1회(`max_plan_revisions`, 0~2), 실행 중 선택자 보정 2회, 전체 작업 180초, 공급자가 보고한 누적 토큰 40,000개. 재작성도 전체 호출·토큰 예산을 공유합니다. 토큰 상한은 다음 호출을 막는 기준으로, 이미 끝난 한 호출의 사용량을 소급 제한하지 못합니다. 사용량 미보고는 0이 아니라 알 수 없음으로 표시하며 호출 수·출력 토큰·이미지 크기 상한은 유지됩니다.

브라우저 예산은 페이지 관찰·모든 보정 실행에 걸쳐 공유됩니다. 기본 총 요청 60개, 분당 60개, 동시 요청 2개, 다운로드 8 MiB, 개별 응답 2 MiB, 관찰 시작 이후 120초입니다. `BudgetLimits`로 허용 범위 안에서 조정할 수 있습니다. 실행 계획은 최대 20동작, 동작 제한 시간은 기본 3초·최대 5초입니다.

주요 코드: `MODEL_CALL_LIMIT`, `MODEL_TOKEN_LIMIT`, `QA_RUNTIME_LIMIT`, `CANCELLED`, `AUTHORIZATION_REVOKED`, `RESET_FAILED`, `STEP_OR_READINESS_FAILED`, `CHECK_TARGET_UNAVAILABLE`, `AMBIGUOUS_CHECK_TARGET`, `ORACLE_AMBIGUOUS`, `EVIDENCE_CAPTURE_FAILED`, `INVALID_PLAN_OR_BASELINE`, `QA_EXECUTION_FAILED`. 공급자 오류 코드는 [분석 API v2](ai-analysis.md)에 정리했습니다.

잘못된 초기 입력·설정·프로그램 ID·시작 전 승인 실패는 예외로 거절하며 모델을 호출하지 않습니다. 실행 중 실패는 가능한 한 저장된 결과와 오류 코드로 반환합니다. 저장소 자체가 실패한 경우 호출자는 파일 I/O 예외를 처리해야 합니다. 호출 태스크를 취소하면 취소 결과를 기록한 뒤 `CancelledError`를 다시 전달합니다.
