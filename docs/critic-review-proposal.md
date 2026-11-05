# 실행 전 비판 모델 검토

Groq 비판자를 선택적으로 연결할 수 있다. 기존 링크를 유지하기 위해 파일 이름은 `critic-review-proposal.md`를 사용하지만, 공급자 어댑터·엔진 흐름·평가 옵션은 구현되어 있다. 기본값은 비활성화이며 설정만으로 외부 공급자에 데이터를 보내지 않는다.

## 흐름과 권한

`제보 해석 → 페이지 관찰 → 계획 작성 → 호스트 검증 + 비판자 1회 → 필요한 경우 작성자 수정 → 호스트 재검증 → 계획 고정 → 브라우저 2회 실행 → 보고`

첫 모델은 이미지·제보를 읽고 계획을 작성한다. 비판자는 **정제된 원문 제보, 현재 DOM 요소·위치·크기, 작성된 제안(작성자의 설득 문구는 제외), 호스트 검증 오류, 공개 실행 환경**을 받는다. 실행 환경은 이미 적용된 화면 크기·브라우저/버전·세션 방식·테스트 컨텍스트 ID·응답 지연·앱 버전이며, 시나리오에 다시 설정 동작을 추가할 필요가 없다. 이미지·세션 헤더·네트워크 로그·평가 정답·독립 채점 결과는 받지 않는다. 비판자에게 외부 도구나 브라우저 실행 권한도 주지 않는다.

검토 대상은 누락된 선행 조작, 순서 오류, 불필요한 클릭, 초기 상태를 사용한 완료 조건, 정상·결함 조건의 뒤바뀜, 입력값 변경, 불필요한 추가 질문이다. 결함을 찾지 못하면 `accept`, 근거 있는 수정이 필요하면 `revise`, 필수 사실이 없으면 `needs_info`를 반환한다. 무조건 반대하거나 상대 모델을 설득할 때까지 토론하지 않는다.

공급자 응답은 `assessment` 안에서 `accept/revise/needs_info`별로 서로 다른 필수 필드를 강제한다. 통과하면서 지적·질문을 붙이거나, 수정 요청인데 지적이 없는 응답은 스키마에서 거절한다. 저장되는 검토 응답은 `decision`, `summary`, `issues`, `questions`로 정규화한다. 각 지적에는 제한된 오류 코드·문제 필드·간결한 설명·`report/page/proposal/validation/environment` 중 근거 참조가 필요하다. 대체 계획이나 버그 판정 필드를 끼워 넣으면 거절한다. 형식이 맞아도 지적이 사실이라는 보장은 없으므로 작성자는 원문과 DOM을 다시 대조한다.

호스트 검증과 비판자가 동시에 오류를 발견하면 **같은 재작성 예산**을 사용한다. 기본 `max_plan_revisions=1`이며 비판자를 이유로 무제한 재시도하지 않는다. 최종 assert, 결과와 같은 readiness, 제보에 없는 입력 등은 비판자의 동의로 우회할 수 없다. 범위 위반은 기존 실행 정책에서 차단한다.

두 모델의 동의는 재현 증거가 아니다. 최종 판정은 고정된 `ready/expected/symptom`을 실제 브라우저에서 두 번 관찰한 결과로 결정한다. 개발자가 수정한 뒤 `retest()`는 같은 계획을 사용하며 비판자와 계획 작성자를 다시 호출하지 않는다.

동작·완료 조건·결과 조건을 바꾸는 지적에는 `report` 근거가 필요하다. `proposal`만 인용하면서 검색 버튼이 불필요하다고 주장하면 수정에 사용하지 않고 `ignored_issues`에 보존한다. `CONTRACT_ERROR`는 `validation`, `UNNECESSARY_QUESTION`은 `page` 근거도 허용한다. 이 필터는 근거 참조의 존재를 검사하며 내용의 진실성까지 보장하지 않는다. 호스트 검증 오류는 이 필터와 관계없이 여전히 수정을 요구한다.

## 서버 연결

```python
from core_engine import GroqModel, OpenAIModel, QAService

service = QAService(
    OpenAIModel.from_env(), policy, store,
    reviewer=GroqModel.from_env(),
)
```

서버 비밀정보 설정으로 `GROQ_API_KEY`를 주입하고 `GROQ_MODEL=openai/gpt-oss-120b`를 지정한다. 키는 코드·PR·로그에 저장하지 않는다. `.env.example`에는 변수 이름만 있으며 자동 `.env` 로딩은 없다. 비판자 해제는 `reviewer=None`이다. 첫 모델이 자신의 계획을 다시 검토하는 대조 실험에는 같은 모델 객체를 `reviewer`에 넣을 수 있다.

Groq 어댑터는 고정 HTTPS 주소의 Chat Completions와 strict JSON Schema를 사용한다. 이미지·도구·리디렉션·암묵적인 HTTP 재시도를 지원하지 않는다. GPT-OSS 모델에는 `reasoning_effort=medium`, `include_reasoning=false`를 사용한다. 응답 내용은 파싱·스키마 검증하며 공급자 오류 본문은 로그로 내보내지 않는다. [Groq 구조화 응답](https://console.groq.com/docs/structured-outputs), [호환 API](https://console.groq.com/docs/openai), [추론 설정](https://console.groq.com/docs/reasoning).

## 결과와 제한

`report.plan_review`는 선택적 추가 필드이며 보고서 버전은 `2`를 유지한다.

| 필드 | 의미 |
|---|---|
| `enabled` | 이 서비스에 비판자가 설정됐는지 |
| `status` | 아래 검토 단계 |
| `review` | 검증된 검토 응답 또는 null |
| `ignored_issues` | 근거 참조가 부족하여 수정에 사용하지 않은 지적. 원본 `review`는 그대로 보존 |
| `reviewed_plan_sha256` | 비판자가 본 유효한 초기 계획 해시. 호스트가 거절한 계획/질문 제안이면 null |
| `final_plan_sha256` | 최종 저장된 실행 계획 해시 또는 null. 실행 중 선택자 보정으로 달라질 수 있음 |
| `error_code` | 비판 호출 실패 코드 또는 null |

상태는 `disabled`, `not_reached`, `reviewing`, `accept/revise/needs_info`, `accepted`, `revised_unreviewed`, `revision_exhausted`, `failed`, `interrupted`, `skipped_retest`다. `accepted`는 초기 검토에서 통과했다는 뜻이다. `revised_unreviewed`는 지적 또는 호스트 검증 후 수정본이 호스트 검사를 통과했으나 **비판 모델의 재검토는 하지 않았다**는 뜻이다. 해시가 다른 최종 계획에 초기 승인을 그대로 붙여 해석하면 안 된다.

`accepted_ignoring_unanchored_review`는 초기 계획이 호스트 검사를 통과했으며 비판자의 지적에 필요한 근거 참조가 없어 초기 계획을 유지했다는 뜻이다. 비판자가 승인했다고 표시해서는 안 된다.

`review_proposal.json`에는 비판자가 받은 정제된 제안과 호스트 오류를 저장하여 검토 내용을 추적할 수 있다.

진행 이벤트에 `reviewing_plan`, `plan_reviewed`가 추가된다. `model_calls`에는 `phase=plan_review`, `role=critic`이 기록되고 나머지는 `role=primary`다. 두 역할 모두 기존 작업당 호출·토큰·시간 예산을 공유한다. 기본 호출 상한 6회는 자동으로 늘어나지 않는다. 보고서 작성 예산이 소진되어도 이미 관찰한 브라우저 판정은 보존한다.

비판자를 선택했는데 인증·한도·통신·응답 검증이 실패하면 실행을 중지하고 실패를 명시한다. 조용히 단일 모델로 대체하거나 검토 성공으로 기록하지 않는다. 운영자는 필요하면 별도 작업을 명시적으로 비판자 없이 실행할 수 있다. 취소·권한 철회 검사는 비판 호출 전후와 대기 중에도 적용된다. 진행 중인 HTTP 요청 자체는 공급자 제한 시간까지 남아 있을 수 있다.

무료 티어는 계정별 사용량 제한이 있으며 무료·무제한을 보장하지 않는다. 서비스 계정의 요금제나 결제 설정을 엔진이 변경하지 않는다. 계정별 정확한 청구액 대신 실제 응답의 토큰·모델·호출 수·오류·시간을 기록한다. [Groq 요금제 안내](https://console.groq.com/docs/billing-faqs), [호출 한도](https://console.groq.com/docs/rate-limits).

## 비교 평가

```bash
# 동일한 코드·사례·반복·호출 상한으로 별도 실행한다.
PYTHONPATH=src python -m core_engine.evaluation --live --critic none --repeats 3 --retest-fixed --max-model-calls 180
PYTHONPATH=src python -m core_engine.evaluation --live --critic groq --repeats 3 --retest-fixed --max-model-calls 180
# 추가 호출 자체의 효과를 비교하려면 같은 모델의 자체 검토도 측정한다.
PYTHONPATH=src python -m core_engine.evaluation --live --critic self --repeats 3 --retest-fixed --max-model-calls 180
```

추가 개발 사례는 동일 옵션에 `--suite transfer`를 붙인다. 모든 결과를 보존하고 기존 사례와 추가 사례는 따로 집계한다. 비판자 실패도 분모에 남기며, 실패한 초기 작업 때문에 수정 후 재검증을 못 한 경우에도 해당 비교 항목을 분모에서 빼지 않는다.

같은 코드의 단일 모델 대비 변화, 모델별 사용량과 시간, 잘못된 비판으로 악화된 사례를 함께 확인한다. 자체 검토 조건을 실행하지 않았다면 두 번째 공급자의 효과와 추가 호출 효과를 분리했다고 주장하지 않는다. 공개 개발 세트의 반복 성공은 다양한 실제 서비스 전체의 안정성을 입증하지 않는다.
