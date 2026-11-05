# 텍스트 분석 API v2

새 스크린샷 QA 흐름은 [QA 연동 명세](qa-pipeline.md)를 사용합니다. 이 문서는 기존 `AnalysisService`를 텍스트 제보·선택적 `BrowserRun` 분석에 단독 사용하는 경우의 계약입니다.

## v1에서 변경된 점

취약점 분류에서 일반 웹앱 QA로 제품 방향을 전환했습니다. `schema_version`은 `2`입니다. `general_bug`, `security_suspected`는 제거했고 아래 유형을 사용합니다. 기존 프론트·백엔드의 enum 처리를 함께 변경해야 합니다. 보안 점수·취약점 확정·코드 수정·PR 생성은 제공하지 않습니다.

| classification | 의미 |
|---|---|
| `functional` | 기능 결과·상태 문제 |
| `visual` | 레이아웃·겹침·표시 문제 |
| `interaction` | 클릭·입력 등 상호작용 문제 |
| `content` | 문구·내용 문제 |
| `performance` | 속도·반응성 관련 제보 |
| `compatibility` | 브라우저·화면 환경 호환성 관련 제보 |
| `needs_review` | 정보 부족·낮은 신뢰도·분석 실패 |

일반 유형의 `review_queue`는 `developer_review`, `needs_review`는 `manual_review`입니다. 실제 큐 등록·메시지 전송은 하지 않습니다. 분류 가능한 유형 모두가 현재 브라우저 조건으로 자동 검증 가능하다는 뜻은 아닙니다.

## 호출과 입력

```python
from dataclasses import asdict
from core_engine import AnalysisService, BugReport, OpenAIModel

report = BugReport(
    report_id="report-123", program_id="project-1", title="검색 결과 미갱신",
    reproduction_steps="cat 검색 후 missing 검색", expected_result="빈 결과", actual_result="cat 결과 유지",
)
result = AnalysisService(OpenAIModel.from_env()).analyze(report)
payload = asdict(result)
# 같은 제보의 실제 BrowserRun을 확보했다면 analyze(report, run)
```

`BugReport`의 ID는 영문·숫자 시작, 영문·숫자·`_ . : -`로 된 최대 128자입니다. 제목은 최대 200자, 재현 순서는 8000자, 기대/실제 결과는 각 4000자이며 빈 문자열을 허용하지 않습니다. 전체 ASCII 이스케이프 JSON은 최대 100 KiB입니다.

`analyze()`는 동기 함수입니다. 비동기 백엔드는 별도 작업 워커 또는 `asyncio.to_thread()`를 사용합니다. 한 번의 호출은 모델 요청 한 번이며 자동 재시도하지 않습니다.

프로그램 ID, 범위 버전, 계획 해시, 시도와 근거 연결, 관찰과 판정의 일관성을 확인합니다. 잘못된 입력은 API 호출 전 `ValueError`입니다. `BrowserRun` 자체에는 제보 ID가 없으므로 동일 프로그램 안에서 같은 제보의 실행인지 연결하는 책임은 백엔드에 있습니다. 새 `QAService`는 작업별 제보 ID와 저장된 계획을 함께 관리합니다.

## 출력

| 필드 | 의미 |
|---|---|
| `schema_version` | `"2"` |
| `report_id`, `program_id` | 입력 식별자 |
| `status` | `completed` / `unavailable` |
| `classification`, `review_queue` | 일반 버그 유형과 권장 검토 담당 |
| `human_review_required` | 항상 true |
| `reproduction`, `observations` | 원본 실행 판정과 근거. 실행 없으면 null / 빈 배열 |
| `assessment` | AI 분석 내용. 실패하면 null |
| `review_reasons` | 엔진이 추가한 검토 사유 |
| `model`, `usage` | 공급자 모델 ID와 input/output 토큰. 미보고는 null |
| `error_code` | 실패 코드 또는 null |
| `limitations` | 결과 해석 제한 사항 |

`assessment`는 분류 신뢰도·분류 이유, 임시 영향도와 이유, 요약, 원인 가설, 해결 방향, 부족한 정보를 포함합니다. 영향도는 `unknown/low/medium/high/critical`이며 사용자 영향에 대한 임시 추정입니다.

가설은 `description`, `source_refs`를 포함합니다. 해결 방향은 `title`, `description`, `source_refs`, `verification_steps`, `tradeoffs`를 포함합니다. 소스 코드를 읽지 않았으므로 실제 파일·함수·패치를 확정하지 않습니다. 정보가 부족하면 추가 진단 단계를 제안합니다.

참조는 `report` 또는 실제 `evidence:<id>`만 허용합니다. `report`는 제보 주장에 대한 참조이지 실행 증거가 아닙니다. 형식과 ID의 존재를 검증해도 문장의 사실성이나 원인의 정확성을 보장할 수는 없습니다.

낮은 분류 신뢰도는 유형과 관계없이 `needs_review`로 변경합니다. 실행이 없으면 `NO_REPRODUCTION_EVIDENCE`, 미재현·판단 불가이면 `REPRODUCTION_NOT_CONFIRMED`를 추가하고 신뢰도는 최대 medium입니다. 낮은 신뢰도에는 `LOW_CLASSIFICATION_CONFIDENCE`가 추가됩니다. confidence는 교정된 확률이 아닙니다.

## 공급자 오류

| error_code | 의미 |
|---|---|
| `MODEL_AUTH_FAILED` | 401/403 인증·권한 오류 |
| `MODEL_RATE_LIMITED` | 429 호출 한도 |
| `MODEL_REQUEST_TOO_LARGE` | 413 요청 하나가 공급자 한도(예: 분당 토큰)보다 큼. 같은 요청은 다시 보내도 실패 |
| `MODEL_HTTP_ERROR` | 그 외 HTTP 오류·리다이렉트 |
| `MODEL_TIMEOUT` | 소켓 통신 제한 시간 초과 |
| `MODEL_UNAVAILABLE` | 네트워크·전송 실패 |
| `MODEL_RESPONSE_TOO_LARGE` | 응답 1 MiB 초과 |
| `MODEL_INVALID_RESPONSE` | API 외피·JSON·사용량 형식 오류 |
| `MODEL_REFUSED` | 생성 거절 |
| `MODEL_INCOMPLETE` | 생성 미완료 |
| `MODEL_INVALID_OUTPUT` | 출력 크기·필드·타입·분류값·근거 참조 오류 |

실패하면 `status=unavailable`, `classification=needs_review`, `assessment=null`로 반환하고 재현 결과를 보존합니다. 오류에 원시 응답이나 인증 헤더를 담지 않습니다. 사용량이 없다고 비용이 0이라는 뜻은 아닙니다.

## 다른 모델 공급자

`JsonModel.generate(*, instructions, context, schema, images=()) -> Generation`을 구현합니다. 이미지 배열은 검증된 `Screenshot` 객체이며 원격 이미지 URL을 직접 받지 않습니다. `Generation`에는 JSON 문자열·모델 ID·선택적 `TokenUsage`를 포함합니다. 공급자 실패는 `ModelError`로 변환하고 스키마·키·고객 데이터를 변경하거나 로그에 유출하지 않습니다.

OpenAI 어댑터는 고정 HTTPS 주소에 JSON Schema와 선택적 이미지 data URL을 전송합니다. 텍스트 요청 부분 128 KiB, 이미지 최대 3개·총 8 MiB, 응답 1 MiB, 분석 JSON 64 KiB 한도입니다. 기본 소켓 제한 시간은 30초, 출력 상한은 3000토큰입니다. API 응답 저장은 `store=false`이며 전체 데이터 정책과 동일하지 않습니다.
