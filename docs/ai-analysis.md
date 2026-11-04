# AI 분류·해결 방안 생성 연동 명세 v1

## 호출 순서와 책임

1. 백엔드에서 프로그램·제보 접근 권한과 외부 AI 전송 허용을 확인하고 고객 데이터를 정제합니다.
2. `BugReport`를 생성합니다. 접수 직후에는 재현 결과 없이 분석할 수 있습니다.
3. 재현을 수행했다면 백엔드가 같은 제보·프로그램의 `BrowserRun`인지 확인합니다.
4. `AnalysisService(model).analyze(report, run=None)`을 호출하고 결과를 저장합니다.
5. 담당자는 분류·임시 심각도·원인 가설·해결 방안을 검토하고 최종 판단합니다.

호출 한 번당 모델 요청은 한 번입니다. 재현 전 분석 후 재현 결과를 추가해 다시 분석하면 두 번의 유료 호출이 될 수 있습니다. 중복 요청 방지, 고객별 호출 예산, 재시도 정책, 큐, 영속 저장, 취소와 외부 워커의 전체 실행 시간 제한은 백엔드 책임입니다. 현재 엔진은 REST 엔드포인트나 자동 개발자 알림을 제공하지 않습니다.

## 입력

`BugReport`는 수정 불가능한 데이터 객체입니다. ID는 영문·숫자로 시작하고 영문·숫자·`_ . : -`로 구성된 최대 128자입니다.

| 필드 | 조건 |
|---|---|
| `report_id` | 백엔드가 부여한 제보 ID |
| `program_id` | 승인된 프로그램 ID |
| `title` | 비어 있지 않은 문자열, 최대 200자 |
| `reproduction_steps` | 제보자의 재현 순서, 최대 8,000자 |
| `expected_result` | 기대 결과, 최대 4,000자 |
| `actual_result` | 관찰했다고 주장하는 결과, 최대 4,000자 |

전체 제보의 ASCII 이스케이프 JSON 크기는 100 KiB 이하입니다. 빈 값·잘못된 ID·길이 초과는 `ValueError`이며 API를 호출하지 않습니다. 고객의 비밀번호·세션·원문 파일·DOM을 제보 텍스트에 그대로 넣지 않습니다.

선택 입력 `run`은 `BrowserRunner`가 반환한 `BrowserRun`입니다. 프로그램 ID, 범위 버전, 계획 해시 형식, 두 시도와 각 근거의 연결, 관찰값과 판정의 일관성을 확인합니다. 불일치하면 API 호출 전 `ValueError`를 발생시킵니다. 이 검사는 근거의 진위를 인증하거나 제보와 계획의 의미적 일치를 증명하지 않습니다. 제보 ID가 없는 `BrowserRun`을 올바른 제보에 연결하는 책임은 백엔드에 있습니다.

## 출력

`dataclasses.asdict(result)`로 JSON 직렬화 가능한 사전을 얻습니다. 튜플은 JSON 배열로 직렬화됩니다.

| 필드 | 의미 |
|---|---|
| `schema_version` | 현재 `"1"` |
| `report_id`, `program_id` | 입력 ID; 모델에는 전송하지 않음 |
| `status` | 유효한 모델 결과 `completed` / 모델 분석 실패 `unavailable` |
| `classification` | `general_bug` / `security_suspected` / `needs_review` |
| `review_queue` | `developer_review` / `security_review` / `manual_review`; 실제 큐 등록은 하지 않음 |
| `human_review_required` | 항상 `true` |
| `reproduction` | 원래 `Verdict`의 `value`, `reason_code`, `evidence_ids`; 실행 없으면 `null` |
| `observations` | 원래 `Evidence` 목록; 실행 없으면 빈 배열 |
| `assessment` | 아래 분석 내용; 실패하면 `null` |
| `review_reasons` | 엔진이 추가한 검토 사유 코드 목록 |
| `model` | 공급자가 반환한 모델 ID; 응답을 얻지 못하면 `null` |
| `usage` | `input_tokens`, `output_tokens`; 공급자가 보고하지 않으면 `null` |
| `error_code` | 분석 실패 코드; 성공하면 `null` |
| `limitations` | 결과 해석 시 표시할 제한 사항 |

`status=completed`는 형식 검증을 통과했다는 뜻이며 버그·보안 위험·원인을 확정했다는 뜻이 아닙니다. JSON 형식과 인용 ID의 존재 여부를 검증해도 문장의 정확성·근거와 주장의 의미적 일치까지 보장할 수는 없습니다. 출력에는 사람이 검토할 내용만 담고, 자동 실행 필드는 없습니다.

### assessment

| 필드 | 의미 |
|---|---|
| `confidence` | 분류 신뢰도 `low` / `medium` / `high`; 교정된 확률이 아님 |
| `classification_reason` | 분류 이유 |
| `severity` | 임시 영향 추정 `unknown` / `low` / `medium` / `high` / `critical`; CVSS 아님 |
| `severity_reason` | 영향 추정 근거와 불확실성 |
| `summary` | 검토용 요약 |
| `hypotheses` | 최대 3개, 각 항목에 `description`, `source_refs`; 확정 원인 아님 |
| `solutions` | 1~3개, 아래 구조; 정보 부족 시 추가 진단 단계 |
| `missing_information` | 최대 6개의 추가 필요 정보 |

각 해결 방안은 `title`, `description`, `source_refs`, `verification_steps`(1~6개), `tradeoffs`(0~4개)를 갖습니다. 일반 버그와 보안 의심 모두 같은 형식을 사용합니다. 보안 제안은 권한 확인·입력 검증 같은 방어적 조치와 승인된 스테이징·합성 데이터에서의 검증 절차를 요구하도록 프롬프트를 구성했습니다. 생성 내용은 여전히 사람의 검토가 필요합니다.

`source_refs`는 `report` 또는 실제 실행 기록의 `evidence:<id>`만 허용합니다. `report`는 제보 내용에 대한 참조이며 재현 증거가 아닙니다. 실행 없이 `evidence:*`를 인용하거나 알 수 없는 ID를 반환하면 전체 분석을 거절합니다. 관찰된 사실은 별도 `observations`를 기준으로 표시합니다.

### 엔진이 적용하는 검토 규칙

- 낮은 신뢰도의 `general_bug` → `needs_review`; `LOW_CLASSIFICATION_CONFIDENCE` 추가.
- 낮은 신뢰도의 `security_suspected` → 보안 검토 유지; 같은 검토 사유 추가.
- 실행 없음 → `NO_REPRODUCTION_EVIDENCE`, 신뢰도 최대 `medium`.
- 실행이 `not_reproduced` 또는 `inconclusive` → `REPRODUCTION_NOT_CONFIRMED`, 신뢰도 최대 `medium`.
- API·응답 실패 → `unavailable`, `needs_review`, `manual_review`, `assessment=null`. 이전 재현 판정과 관찰 기록 보존.

## 실패와 재시도

| 오류 코드 | 의미 |
|---|---|
| `MODEL_AUTH_FAILED` | API 인증·권한 오류(401/403) |
| `MODEL_RATE_LIMITED` | 공급자 호출 한도(429) |
| `MODEL_HTTP_ERROR` | 다른 HTTP 오류 또는 리다이렉트 |
| `MODEL_TIMEOUT` | 소켓 통신 제한 시간 초과 |
| `MODEL_UNAVAILABLE` | 네트워크·전송 실패 |
| `MODEL_RESPONSE_TOO_LARGE` | API 응답 1 MiB 초과 |
| `MODEL_INVALID_RESPONSE` | API 응답 외피·JSON·사용량 형식 오류 |
| `MODEL_REFUSED` | 공급자가 생성을 거절 |
| `MODEL_INCOMPLETE` | 출력 한도 등으로 생성 완료되지 않음 |
| `MODEL_INVALID_OUTPUT` | 분석 JSON 크기·필수 필드·타입·분류값·근거 참조 오류 |

오류에 공급자 응답 본문·인증 헤더를 포함하지 않습니다. 자동 재시도는 없으며, 결과가 보류돼도 호출 비용이 발생했을 수 있습니다. 백엔드에서 재시도할 때는 원인·예산·중복 작업을 확인합니다. 잘못된 입력·설정의 `ValueError`와 사용자 정의 공급자 구현의 예상하지 못한 프로그래밍 오류는 호출자에게 예외로 전달합니다.

## 모델 공급자와 제한

`OpenAIModel(api_key=..., model=..., timeout_seconds=30, max_output_tokens=3000)`을 사용하거나 환경 변수로 `OpenAIModel.from_env()`를 호출합니다. 허용 통신 제한 시간은 0초 초과~60초, 출력 한도는 512~8,192토큰입니다. 통신 제한 시간은 작업 전체의 엄격한 종료 시간이 아니므로 외부 워커 감독이 필요합니다.

OpenAI 어댑터는 `https://api.openai.com/v1/responses`에만 요청하며 리다이렉트·실행 도구·자동 재시도를 사용하지 않습니다. API 요청은 최대 128 KiB, 응답은 최대 1 MiB, 분석 텍스트는 최대 64 KiB입니다. `store=false`로 전송하지만 공급자의 전체 보존·훈련·지역 정책은 별도로 확인해야 합니다.

다른 공급자는 `JsonModel.generate(*, instructions, context, schema) -> Generation`을 구현합니다. `Generation`은 JSON 텍스트, 모델 ID, 선택적 `TokenUsage`를 포함합니다. 공급자 실패는 `ModelError`의 허용된 코드로 변환해야 합니다. 공급자는 주어진 스키마를 변경하지 않고, 원시 오류·키·제보를 로그에 남기지 않도록 구현합니다. 분석 서비스는 공급자와 관계없이 다시 출력 검증을 수행합니다.

## 검증 범위

자동 테스트는 모의 모델/API와 로컬 브라우저를 사용합니다. 비용이 드는 실제 모델 호출, 한국어 분류 정확도, 오탐·누락률, 근거 인용의 의미적 정확성, 해결 방안의 실제 수정 효과는 아직 평가하지 않았습니다. 초기 운영 전 합성 일반 버그·보안 의심·모호한 제보를 포함한 평가와 사람의 검토가 필요합니다.
