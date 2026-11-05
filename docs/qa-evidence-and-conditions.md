# 실패 진단·재현 영상·조건 비교 연동

엔진은 Python 라이브러리입니다. 아래 함수와 결과 필드를 백엔드 작업 큐·HTTP API·관제 화면에 연결합니다. 고객 코드를 수정하거나 고객 저장소에 PR을 만들지 않습니다.

## 단일 작업의 실패 진단

`QAService.run()`, `resume()`, `retest()`의 `result.report`에 다음 필드가 추가됩니다. 기존 결과의 `schema_version="2"`를 유지하는 추가 필드입니다. 예전 저장 작업에는 새 필드가 없을 수 있으므로 프론트는 기본값을 사용합니다.

| 필드 | 의미 |
|---|---|
| `diagnosis.category`, `summary` | 재현·미관찰·정보 부족·환경 장애·실행 제한 등과 한국어 설명 |
| `diagnosis.reason_codes` | 실제 실행/모델 오류 코드 |
| `diagnosis.attempts[]` | 단계, 완료 동작 수, 막힌 동작의 0부터 시작하는 인덱스, 다음 동작, 관찰 ID, 소요 시간 |
| `diagnosis.observed_facts[]` | 관찰된 요청 방식·정제 URL·HTTP 코드·관찰 ID |
| `diagnosis.missing_information` | 제보자에게 필요한 추가 질문 |
| `diagnosis.next_actions[]` | 처리 담당(`owner`), 안내(`message`), 필요한 필드(`required_fields`) |
| `diagnosis.hypotheses` | AI 원인 가설. 관찰 사실과 별도 표시 |
| `diagnosis.root_cause_confirmed` | 현재 항상 `false`. 블랙박스 QA만으로 코드 원인을 확정하지 않음 |
| `run.evidence[].timeline` | 동작·완료 대기·결과 관찰의 상대 시간 |
| `run.evidence[].completion_evidence` | 마지막 동작 전 완료 표시 존재 여부와 이후 요청·결과·완료 표시 갱신 관찰 |
| `environment.browser_name`, `browser_version` | 실제 실행 엔진과 관찰한 버전. 브라우저가 시작하지 못하면 버전은 `null` |
| `timings` | 작업 전체와 모델 호출 소요 시간. 인건비 절감률 아님 |
| `scenario_contract.required_actions` | 원문 인용으로 고정한 필수 동작과 입력값 |
| `scenario_contract.reported_observations` | 결과 읽기·완료 확인 등 관찰 문구. 추가 클릭이나 페이지 이동으로 실행하지 않음 |
| `scenario_contract.completion_signal` | 원문에 명시된 완료 표시. 정상/결함 결과 조건과 분리 |
| `scenario_contract.reported_actions_complete` | 화면 관찰 전 해석 모델이 제안한 완전성. 이것만으로 추가 동작을 금지하지 않음 |
| `scenario_contract.actions_complete`, `completeness_basis` | 초기 해석과 화면 기반의 유효한 계획이 순서·횟수까지 일치할 때만 전체 순서를 고정. 그 외에는 인용한 필수 동작의 순서를 보존하는 부분 시나리오 |

접수 단계의 잘못된 “완전한 순서” 판정이 검색 제출이나 필터 클릭을 막지 않도록 페이지 기반 계획과 대조한 후 확정합니다. 두 단계의 일치가 실제 제보의 완전성을 보장하는 것은 아닙니다. 동일한 조작을 연속 반복하려면 제보에서 추출한 반복 동작이 필요하며, `setup_steps` 마지막과 `final_action`에 같은 트리거를 중복해 넣으면 실행 전에 거절합니다.

마지막 동작 전에 이미 완료 문구가 보이고 이후 새 요청·결과 변화·완료 표시 갱신이 모두 없으면 `STALE_COMPLETION_SIGNAL`로 판정을 보류합니다. 입력만 바꾸고 제출하지 않은 계획이 이전 결과를 재현 증거로 사용하는 오류를 막습니다. 이는 동작과 결과의 인과관계 전체를 증명하는 기능은 아닙니다. 처리 시에도 완료 표시를 갱신하지 않는 화면에서는 더 구체적인 완료 조건이 필요할 수 있습니다.

Groq 429의 유효한 `Retry-After` 초 값은 `model_calls[].retry_after_seconds`와 `diagnosis.next_actions[].retry_after_seconds`로 전달합니다. 엔진은 자동 재시도하지 않으며 비판자 실패를 무시하지 않습니다. 백엔드가 공급자 단위 대기·유한한 재시도를 담당해야 합니다. 이 값이 없는 경우 대기 시간을 추측하지 않습니다. [Groq 공식 문서](https://console.groq.com/docs/rate-limits)에 따르면 제한은 조직 단위이므로 같은 조직의 키 교체로 한도가 늘어나지는 않습니다.

비판 모델의 근거 없는 동작 삭제를 제외한 내역은 `plan_review.ignored_issues`에 남깁니다. `accepted_ignoring_unanchored_review`는 초기 계획을 유지했다는 뜻이며, 두 모델이 동의했다는 표시가 아닙니다. 상세 상태는 [비판 모델 연동 명세](critic-review-proposal.md)를 따릅니다.

`not_reproduced`는 테스트 조건에서 정상 동작을 관찰했다는 뜻입니다. 버그가 없거나 고쳐졌다고 자동 종결하지 않습니다. `inconclusive`, `needs_info`, 취소도 별도 상태로 유지합니다. 로그인 화면이나 401/403만으로 권한 문제가 근본 원인이라고 확정하지 않습니다.

## 영상과 다운로드 테스트

```python
options = BrowserOptions(record_video=True, video_fps=4, video_max_seconds=60)
service = QAService(model, policy, store, options=options, reviewer=critic)
```

`pip install ".[browser,video]"`는 별도 ffmpeg 바이너리를 포함하는 영상 의존성을 설치합니다. `FFMPEG_BINARY`로 신뢰된 실행 파일을 지정할 수도 있습니다. 명시 경로 → 영상 패키지 바이너리 → 시스템 ffmpeg 순으로 찾으며 PNG 디코딩과 VP9 인코딩이 필요합니다. 기본 영상 옵션은 꺼져 있으며, 활성화하면 초기 관찰(`attempt=0`)과 재현 시도(`attempt=1,2`), 보정 후 시도를 각각 기록합니다.

영상은 **마스킹한 화면을 기본 초당 4회 샘플링한 WebM**입니다. 원본 Playwright 녹화는 스크린샷 마스크를 자동 적용하지 않으므로 사용하지 않습니다. 음성은 없고 모든 순간·모든 프레임을 보장하지 않습니다. 스크린샷에도 적용되는 입력칸·민감 선택자·알려진 개인정보 마스크를 사용하지만 모든 개인정보를 탐지하지는 않습니다. 승인된 합성 데이터를 사용합니다.

`videos[]`는 `attempt`, `revision`, `plan_sha256`, `status`, `sampling_fps`, `truncated`, `capture_error`, `artifact`를 제공합니다. 준비되면 `start_offset_seconds`, `duration_seconds`가 추가됩니다. `artifact`는 `name`, `mime_type`, `bytes`, `sha256`입니다. `ready`만 재생하고 `failed/unavailable/empty/interrupted`는 명시적으로 표시합니다. `truncated=true`는 부분 영상입니다. 영상 실패가 이미 관찰한 재현 판정을 바꾸지는 않습니다. 작업 자체의 취소·실행 시간 제한은 여전히 적용됩니다.

화면 타임라인과 영상 시간은 `영상 위치 ≈ evidence 상대 시간 - start_offset_seconds`로 연결합니다. 촬영/처리 지연이 있어 대략적인 위치이며 정확한 오류 프레임으로 표시하지 않습니다. 영상당 최대 8 MiB, 작업당 영상 24 MiB, 프레임 메모리 16 MiB, 최대 120초/600샘플로 제한합니다.

버그가 재현되면 `test_export.status="ready"` 및 `reproduction.spec.ts`를 제공합니다. 공개 함수 `export_playwright_test(plan, policy=policy, options=options)`로도 내보낼 수 있습니다. AI가 JavaScript 코드를 작성하는 대신 검증된 동작·조건을 고정 템플릿으로 변환합니다. **정상 동작을 assert하므로 버그에서 실패, 수정 후 통과하는 회귀 테스트**입니다. 별도 컨텍스트에서 2회 실행합니다.

```bash
npm install --save-dev @playwright/test@1.64.1
npx playwright install chromium
QA_RESET_MODULE=/absolute/path/reset.mjs npx playwright test reproduction.spec.ts
```

`reset.mjs`는 `export async function reset(attempt) { ...; return true; }`를 제공해야 합니다. 서버 데이터가 있다면 실제 초기화를 수행합니다. 빈 초기화로 서버 상태가 초기화된 척하면 안 됩니다. 인증이 필요한 테스트는 `QA_AUTHORIZATION`, `QA_COOKIE`, `QA_STORAGE_STATE` 중 등록 방식에 맞는 값을 비밀정보로 주입합니다. 원래 URL에 쿼리가 있으면 `QA_ENTRY_URL`/`QA_NAVIGATE_N`이 필요합니다. `test_export.required_environment`에 요구 사항을 반환하며 비밀 값·쿼리 원문은 소스에 넣지 않습니다.

독립 실행용 테스트는 원점/경로/읽기 전용 요청 검사를 포함하지만 엔진의 DNS/IP 고정 전송과 동일한 격리 환경은 아닙니다. 승인된 격리 테스트 환경에서 실행합니다. 테스트의 성공도 실제 제품 요구사항을 대신하지 않습니다.

## 조건별 비교

```python
from core_engine import BrowserOptions, TestCondition, ConditionsService, ConditionLimits

conditions = (
    TestCondition("admin", "관리자 · Chromium", BrowserOptions(
        test_context_id="admin-fixture-v1", session_headers=admin_test_headers)),
    TestCondition("staff", "일반 직원 · Chromium", BrowserOptions(
        test_context_id="staff-fixture-v1", session_headers=staff_test_headers, record_video=True)),
    TestCondition("staff_firefox", "일반 직원 · Firefox", BrowserOptions(
        browser_name="firefox", test_context_id="staff-fixture-v1", session_headers=staff_test_headers)),
    TestCondition("no_session", "세션 없음", BrowserOptions()),
)
matrix = ConditionsService(model, policy, store, conditions, reviewer=critic,
    limits=ConditionLimits(max_conditions=4, max_model_calls=24, max_runtime_seconds=600))

# reset_condition(condition_id, ordinal): 0=초기 관찰, 1/2=독립 재현 시도
result = await matrix.run(request, reset=reset_condition, is_authorized=check_authorization)
# 등록 순서대로 고정 비교하려면 select=False
verified = await matrix.retest(result.job_id, "staff", reset=reset_condition,
    is_authorized=check_authorization)
```

등록 조건은 백엔드가 만드는 신뢰된 값입니다. AI는 제공된 ID만 선택하고 가설을 적습니다. 계정을 생성하거나 권한·범위·브라우저 설정을 임의로 바꾸지 않습니다. 필수 계정이 없으면 질문합니다. 역할·세션·화면 크기·브라우저·응답 지연을 명시적으로 조합합니다. `response_delay_ms`는 각 응답 전달에 0~2000ms를 추가하며 대역폭 제한·패킷 손실 시뮬레이터는 아닙니다.

`python -m playwright install chromium firefox webkit`으로 사용할 브라우저를 설치합니다. Linux 시스템 라이브러리도 준비합니다. 실제 브라우저 엔진을 실행하지만 전송 계층의 User-Agent 등 모든 헤더를 해당 사용자의 원래 브라우저와 동일하게 재현하지는 않습니다. 미설치/시작 실패는 `BROWSER_UNAVAILABLE`입니다.

조건 작업은 `schema_version="conditions-1"`, `job_kind="condition_matrix"`입니다. `conditions[]`에 자식 `job_id`, 조건, 가설, 상태, 재현 판정, 진단, 영상, 테스트, 소요 시간을 둡니다. `matched_condition_ids`는 두 번 재현된 조건만 포함합니다. `coverage`로 등록·선택·실행 완료 수 및 선택되지 않은 ID를 제공합니다. 실행 전 중단된 항목은 `not_run`; 시작된 작업은 취소되더라도 자식 작업 ID와 증거를 유지합니다. `most_progressed_condition_ids`는 **완료 동작 수 기준**이지 증상과 의미적으로 가장 유사하다는 점수가 아닙니다.

모델/비판자/조건 선택은 전체 호출 예산을 공유합니다. 사용량 제한·공급자 인증 실패·취소·권한 철회에는 남은 조건을 멈춥니다. 재검증은 같은 자식 계획·화면 크기·컨텍스트·세션 방식·브라우저와 버전·지연 설정을 사용합니다. 앱 버전은 수정 배포 검증을 위해 달라질 수 있습니다. 계정의 실제 역할/데이터가 같은지는 백엔드가 보장해야 합니다.

## 백엔드와 프론트의 연결 책임

1. 기존 작업 생성 API에 `registered_condition_ids`, `record_video` 요청을 받아 서버 설정과 대조합니다. 사용자가 세션 헤더·스토리지 파일 경로를 직접 제출하는 API로 만들지 않습니다.
2. 부모 작업의 `events.json`, `conditions.json`을 폴링하거나 기존 이벤트 스트림으로 전달합니다. 완료 후 `result.json`이 최종 결과입니다. 단일 작업은 기존 `events.json`을 사용합니다.
3. 아티팩트는 고객·프로젝트·작업 소유권을 확인한 다운로드 API로 제공합니다. WebM은 `video/webm`, 바이트 범위 요청, 짧은 유효기간 URL을 지원하면 플레이어 탐색에 유리합니다. 로컬 경로를 클라이언트에 그대로 노출하지 않습니다.
4. 조건 비교 표, 실패 이유/필요 정보, 시도별 타임라인, 영상, 테스트 다운로드를 연결합니다. 영상 없음·부분 영상·미실행을 빈 성공 화면으로 처리하지 않습니다.
5. 저장된 계획 재검증을 별도 작업으로 생성합니다. 사람이 수정 배포 후 요청합니다. 원 보고서와 `plan_sha256` 연결을 유지합니다.

첨부 목업의 정확한 UI·7개 시나리오·trace 다운로드는 이 라이브러리 변경으로 제공되는 프론트 기능이 아닙니다. 현재는 제한된 화면/요청 증거를 제공하며 원본 trace/HAR는 생성하지 않습니다. POST 결제와 파일 업로드는 지원하지 않습니다. 등록된 테스트 계정의 폼 로그인은 0.4.0부터 [별도 호스트 설정](registered-test-login.md)으로 지원합니다.
