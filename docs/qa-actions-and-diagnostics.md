# 0.7.0 재현 동작과 진단

## 변경 이유

새로고침·뒤로 가기 후에 나타나는 결함은 페이지 이동으로 대체하면 다른 시나리오가 됩니다. 클릭 여부를 텍스트만으로 구분하기 어려운 UI도 있습니다. 이 버전은 선언형 동작·검사 범위를 확장하고, 실제 브라우저 중단 원인과 후속 모델 오류를 함께 전달합니다.

## 동작 계약

| action | target | value | 동작 |
|---|---|---|---|
| `reload` | 빈 문자열 | 저장/모델 JSON은 빈 문자열, Python Step은 None | 현재 페이지 실제 새로고침 |
| `back` | 빈 문자열 | 위와 동일 | 브라우저 방문 기록으로 한 단계 뒤로 |

```python
Step("reload", "")
Step("back", "")
```

첫 단계는 계속 호스트가 등록한 `navigate`입니다. AI가 추출한 `required_actions`의 원문 인용과 동작 순서에 새로고침/뒤로 가기가 있어야 계획에 추가할 수 있습니다. UI의 ‘뒤로’ 버튼 클릭과 브라우저 뒤로 가기는 다릅니다. 방문 기록이 없으면 `HISTORY_UNAVAILABLE`로 판정을 보류하며 새로 이동한 것으로 성공 처리하지 않습니다. 같은 URL의 `pushState`도 실제 기록 이동으로 처리합니다.

두 번의 독립 실행·새 세션·reset, 요청의 원점/경로/DNS/TLS/메서드 제한을 유지합니다. HTTP 요청 없이 이동하는 SPA history도 이동 후 경로를 검사하며 범위 밖 화면은 캡처하지 않습니다. 페이지 이동 제한은 `navigation_timeout_ms`입니다. 새 동작은 타임라인·마스킹 증거·영상·라이브 이벤트와 생성한 TypeScript 수정 확인 테스트에도 반영합니다.

## 검사 계약

```json
{"kind":"url_path","selector":"","value":"/cart","other_selector":""}
```

현재 URL의 **pathname만 정확하게** 비교합니다. 쿼리·프래그먼트·외부 원점·정규식·부분 일치는 지원하지 않습니다. 기대 경로도 승인된 범위에 있어야 합니다. 경로만 도착했다고 데이터 처리가 완료됐다는 뜻은 아니므로, 데이터 검사에는 별도의 완료 표시가 필요합니다.

```json
{"kind":"attribute","selector":"[data-testid=wishlist]","value":"true","other_selector":"aria-pressed"}
```

`other_selector`는 이 종류에서 속성 이름입니다. `aria-pressed/aria-checked/aria-expanded/aria-selected/aria-busy/aria-disabled`만 허용하며 값은 `true/false`, pressed/checked만 `mixed`도 허용합니다. 임의 속성·input 값·스크립트를 읽지 않습니다. 속성이 없거나 허용하지 않는 값이면 `CHECK_ATTRIBUTE_UNAVAILABLE`로 보류합니다. 없음을 false로 추측하지 않습니다. DOM 관찰의 `elements[].state_attributes`에는 위 허용 값만 포함됩니다.

ready·expected·symptom은 계속 별개입니다. 짧은 문구를 `body/html/*` 전체와 정확히 비교하는 모델 계획은 `PLAN_OVERBROAD_TEXT_TARGET`으로 실행 전에 거절하고 기존 제한 안에서 수정을 요청합니다. 실제 상태/가격/행의 선택자를 사용해야 하며 기대 문구를 임의로 바꾸지 않습니다.

## 오류·모델 기록

- `TLS_CERTIFICATE_ERROR`: 인증서 검증 실패. 검증을 끄거나 HTTP로 바꾸지 않습니다.
- `TLS_HANDSHAKE_ERROR`, `CONNECTION_REFUSED`, `CONNECTION_RESET`: 정제된 연결 단계 오류. 원문 예외·응답·비밀정보는 전달하지 않습니다.
- 기존 `TRANSPORT_ERROR/TRANSPORT_TIMEOUT`도 계속 지원합니다. 세분화할 수 없는 오류는 원인을 추정하지 않습니다.
- `MODEL_RATE_LIMITED`: 공급자 한도. `MODEL_CALL_LIMIT/MODEL_TOKEN_LIMIT`: 엔진 작업별 예산. 후자는 `diagnosis.category=model_budget_exhausted`로 구분합니다.
- `model_calls[].configured_model`: 호출에 설정한 모델. 성공 응답의 `model`과 별도이며 실패 때 `model/usage`는 모를 수 있습니다.
- `model_limit_events[]`: 예산 때문에 **시작하지 않은 호출**의 `phase/role/configured_model/error_code/call_started=false`. 실제 호출 수·성공 건수에 합산하지 않습니다.
- `diagnosis.model_failures[]`: 후속 모델 실패 및 예산 거절을 단계·역할과 함께 제공합니다. 브라우저가 이미 실패한 뒤 repair/report 모델이 실패해도 브라우저 중단 원인을 진단 제목에서 지우지 않습니다. 취소·권한 철회는 계속 우선합니다.

`run.evidence`, 기존 `error_code`, 모든 `reason_codes`를 함께 보존해야 합니다. 성공 verdict가 있어도 후속 보고 모델은 실패할 수 있고, 상세 진단은 자동 생성됩니다. 비판자 비활성은 호스트가 reviewer=None으로 정하는 기존 동작을 유지하며 공급자 오류 후 자동으로 평가 조건을 바꾸지 않습니다.

## 백엔드·프론트 적용

1. 0.7.0 wheel/커밋을 실제 워커에 고정하고 재배포합니다. 0.6.0의 상태 변경 승인·실제 BugLab reset 연결은 계속 필요합니다([백엔드 #102](https://github.com/team-METAPHOR/Metaphor-back/issues/102)).
2. scenario/보고서/라이브 중계의 action enum에 `reload/back`, check enum에 `url_path/attribute`를 허용하고 새 모델 기록을 저장·전달합니다. 새 필드는 과거 보고서에서 생략될 수 있습니다.
3. ‘새로고침/뒤로 가기’ 라벨과 새 진단을 표시합니다. 모델 실패를 브라우저 실패나 ‘Groq 제한’ 하나로 합치지 않습니다.
4. 라이브 **뷰어**의 Vercel/API 인증 계약은 별개입니다([백엔드 #107](https://github.com/team-METAPHOR/Metaphor-back/issues/107), [프론트 #41](https://github.com/team-METAPHOR/Metaphor-front/issues/41)). 이 엔진 변경만으로 배포 WSS 연결이 해결되지는 않습니다.

## 검증 범위와 후속 작업

회귀 테스트는 로컬 결함·정상 대조 화면, 실제 브라우저와 생성된 Playwright 코드로 확인합니다. 모델 관련 회귀는 고정 응답을 사용하며 실제 AI 정확도 수치가 아닙니다.

[엔진 #20](https://github.com/team-METAPHOR/Core_engine/issues/20)의 전체 목표를 완료했다는 뜻은 아닙니다. 초기 화면에서 보이지 않는 후속 화면의 여러 조작을 관찰하며 계획하는 기능은 여전히 제한적이며, 기존 수리는 실패 화면의 선택자 교체에 한정됩니다. 일반적인 속성·URL ready의 신선도만으로 비동기 작업 완료를 보장하지 않습니다. 최종 데모 사례별 완료 조건을 명시해야 합니다. 주환님 버그 목록/정상 대조가 확정되는 [BugLab #12](https://github.com/team-METAPHOR/BugLab-shop/issues/12) 이후 나머지 사례를 점검하고 [엔진 #21](https://github.com/team-METAPHOR/Core_engine/issues/21)에서 실제 AI 성능을 재측정합니다. 100% 재현·인건비 절감률을 이 테스트로 주장하지 않습니다.
