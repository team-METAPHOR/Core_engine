# Groq 비판자 연동 및 QA 재평가 — 2026-11-05

실제 OpenAI 작성 모델과 Groq 비판 모델을 호출하고 Chromium으로 검증한 결과다. 공개된 합성 개발 사례 20개를 조건별 3회 반복했다. 조건별 최초 작업 60건이며 전체 120건이다. 수정 후 재검증은 별도로 집계한다.

## 비교 결과

| 평가 | 같은 개선 코드·비판자 없음 | 같은 개선 코드·Groq 비판자 |
|---|---:|---:|
| 기존 12개 × 3회 전체 통과 | 32/36 | 35/36 |
| 기존 12개 × 3회 · 제보 절차까지 일치한 버그 재현 | 10/12 | 11/12 |
| 기존 12개 × 3회 · 정상 대상 판정 | 10/12 | 12/12 |
| 기존 12개 × 3회 · 정상 대상 오탐 | 0/12 | 0/12 |
| 기존 12개 × 3회 · 동일 계획 수정 확인 | 10/12 | 11/12 |
| 추가 8개 × 3회 전체 통과 | 20/24 | 22/24 |
| 추가 8개 × 3회 · 제보 절차까지 일치한 버그 재현 | 10/12 | 12/12 |
| 추가 8개 × 3회 · 정상 대상 판정 | 10/12 | 10/12 |
| 추가 8개 × 3회 · 정상 대상 오탐 | 1/12 | 0/12 |
| 추가 8개 × 3회 · 동일 계획 수정 확인 | 10/12 | 12/12 |
| 합계 최초 작업 통과 | 52/60 | 57/60 |

**브라우저 검사 복구:** 추가 세트 Groq 실행의 마지막 수정 후 독립 검사에서 브라우저 시작 오류 1건이 있었다. 원래 QA 결과는 저장되어 있어, 같은 AI 응답·계획을 유지한 채 모든 저장된 계획의 독립 브라우저 검사를 다시 수행했다. 추가 모델 호출은 0회이며 기존에 채점된 실패와 성공은 모두 유지됐는지 대조했다. 원본의 `valid_for_comparison=false` 기록과 복구 근거를 함께 보관한다. 새로운 AI 표본을 뽑아 실패를 교체한 결과가 아니다.

이전 PR #6의 기존 세트는 33/36, 추가 세트는 1회 반복에서 5/8이었다. 이번 추가 세트는 3회 반복이므로 과거 5/8과 표본 수가 다르다. 위 두 열은 같은 코드·모델·사례·반복·채점 기준으로 비교했다.

## 실제 변경

- 작성 모델의 준비 단계와 마지막 조작을 분리했다. 마지막 결과 강제 단계는 생성 스키마에서 허용하지 않으며, 클릭·이동에 입력값을 붙일 수도 없다.
- 최초 등록 페이지 이동을 엔진이 보장한다. 모델이 이동을 빠뜨리더라도 새 브라우저의 빈 페이지에서 조작하지 않는다.
- 선행 조작·리터럴 입력·정확한 클릭 횟수·처리 완료 신호를 보존하도록 지침과 재작성 피드백을 보강했다. 재작성에는 생성 형식과 같은 계획 구조를 제공한다.
- Groq 비판자는 실행 전 한 번만 검토한다. 검토 지적과 호스트 오류는 같은 제한된 재작성 예산을 사용하며 실제 판정은 브라우저 관찰이 결정한다.
- 승인·수정 요청·추가 질문의 응답 구조를 분리하여 모순된 검토 응답을 차단한다. 호스트 검증, 취소·권한 철회·두 모델 공통 예산을 적용한다.
- 저장된 시나리오와 동일 계획 재검증 계약은 유지했다. 고객 소스 코드 수정과 고객 PR 생성은 수행하지 않는다.

## 조건과 검증

- 고정 엔진 커밋: `3752aff7a062dce2b7151b9d1ea53f1a8bc0d7ad`.
- 엔진 소스 SHA-256: `cc611f9bfc62727b19f7bc018e8a683950dec286fd394267b0943db93880d67c`.
- 주 모델: `gpt-5.4-mini` 설정. 실제 응답 모델: `gpt-5.4-mini-2026-03-17`, `openai/gpt-oss-120b`.
- 비판자: Groq `openai/gpt-oss-120b`, medium 추론, 텍스트 전용 strict JSON Schema.
- 조건당 버그 24건·정상 24건·정보 부족 6건·환경 장애 6건이며, 같은 사례의 반복은 독립적인 새 문제 수가 아니다.
- 두 조건 모두 seed 17, 3회 반복, 평가 묶음별 전체 공급자 호출 상한 180, 작업당 기본 상한 6회다. 평가 중 코드를 변경하지 않았다.
- 사례 정의·제보·합성 웹앱·독립 채점 파일은 PR #6 이후 바뀌지 않았음을 파일 내용으로 확인했다. 모델에 정답·사례 ID·독립 채점 결과를 보내지 않는다.
- 전체 자동 테스트 110개 통과. 마지막 피드백 형식 조정 뒤 관련 테스트 15개, 최종 URL 정제 보강 뒤 관련 테스트 16개도 통과했다. 자동 테스트에는 실제 API를 넣지 않는다.
- 마지막 16개 검사 첫 실행은 계획 작성 전 페이지 관찰에서 1건 중단됐다. 새 비판자 테스트의 동작 제한 시간을 150ms에서 1,000ms로 조정한 뒤 16개가 통과했다. 엔진 기본값·실제 평가 제한 시간·실험 점수는 변경하지 않았다.
- 최종 비교 작업은 순차 실행했다. 이미지 준비와 독립 재실행은 QA 시간 지표에서 제외하며, 호스트 부하를 통제한 성능 벤치마크는 아니다.
- 평가 후 외부 모델에 전달하는 자동 초기 이동 URL의 쿼리 값을 제거하도록 1줄 보강했다. 실제 브라우저에는 등록된 원래 URL을 사용한다. 평가 요청 163건은 모두 쿼리가 없어 이번 입력·계획·판정에는 영향을 주지 않는다. 성능 측정 커밋과 최종 보호 조치는 구분한다.

## 사례별 결과

| 사례 | 비판자 없음 | Groq 비판자 |
|---|---:|---:|
| `search_bug` | 2/3 | 2/3 |
| `search_normal` | 2/3 | 3/3 |
| `filter_bug` | 3/3 | 3/3 |
| `filter_normal` | 2/3 | 3/3 |
| `clipping_bug` | 2/3 | 3/3 |
| `clipping_normal` | 3/3 | 3/3 |
| `overlap_bug` | 3/3 | 3/3 |
| `overlap_normal` | 3/3 | 3/3 |
| `missing_expectation` | 3/3 | 3/3 |
| `missing_search_data` | 3/3 | 3/3 |
| `environment_unavailable` | 3/3 | 3/3 |
| `environment_unauthorized` | 3/3 | 3/3 |
| `activation_bug` | 2/3 | 3/3 |
| `activation_normal` | 3/3 | 3/3 |
| `quantity_bug` | 2/3 | 3/3 |
| `quantity_normal` | 2/3 | 2/3 |
| `dismissal_bug` | 3/3 | 3/3 |
| `dismissal_normal` | 2/3 | 3/3 |
| `collection_bug` | 3/3 | 3/3 |
| `collection_normal` | 3/3 | 2/3 |

## 미통과 항목

- `core/none/search_normal/1`: INDEPENDENT_ORACLE_FAILED; 오류 없음; 실행 차단 없음; 검토 상태 disabled.
- `core/none/filter_normal/2`: INDEPENDENT_ORACLE_FAILED; 오류 없음; 실행 차단 없음; 검토 상태 disabled.
- `core/none/clipping_bug/3`: VERDICT_MISMATCH; 오류 없음; 실행 차단 없음; 검토 상태 disabled.
- `core/none/search_bug/3`: INDEPENDENT_ORACLE_FAILED; 오류 없음; 실행 차단 없음; 검토 상태 disabled.
- `transfer/groq/quantity_normal/3`: VERDICT_MISMATCH; 오류 없음; 실행 차단 STEP_OR_READINESS_FAILED; 검토 상태 accepted.
- `transfer/groq/collection_normal/2`: VERDICT_MISMATCH; 오류 없음; 실행 차단 STEP_OR_READINESS_FAILED; 검토 상태 revised_unreviewed.
- `transfer/none/quantity_bug/2`: VERDICT_MISMATCH; 오류 없음; 실행 차단 STEP_OR_READINESS_FAILED; 검토 상태 disabled.
- `transfer/none/activation_bug/1`: INDEPENDENT_ORACLE_FAILED; 오류 없음; 실행 차단 없음; 검토 상태 disabled.
- `transfer/none/dismissal_normal/1`: INDEPENDENT_ORACLE_FAILED; 오류 없음; 실행 차단 없음; 검토 상태 disabled.
- `transfer/none/quantity_normal/2`: VERDICT_MISMATCH; 오류 없음; 실행 차단 없음; 검토 상태 disabled.
- `core/groq/search_bug/1`: VERDICT_MISMATCH; 오류 없음; 실행 차단 STEP_OR_READINESS_FAILED; 검토 상태 revised_unreviewed.

## 비판 검토와 사용량

| 조건 | 역할 | 실제 호출 | 입력 토큰 | 출력 토큰 | 사용량 미보고 |
|---|---|---:|---:|---:|---:|
| core/none | primary | 96 | 280378 | 50072 | 0 |
| core/none | critic | 0 | 0 | 0 | 0 |
| transfer/groq | primary | 91 | 291188 | 51054 | 0 |
| transfer/groq | critic | 24 | 52094 | 17720 | 0 |
| transfer/none | primary | 84 | 249142 | 46861 | 0 |
| transfer/none | critic | 0 | 0 | 0 | 0 |
| core/groq | primary | 107 | 339644 | 53705 | 0 |
| core/groq | critic | 24 | 50255 | 19297 | 0 |

| 최초 작업 QA 시간 중앙값(초) | 비판자 없음 | Groq 비판자 |
|---|---:|---:|
| core | 12.3795 | 14.5295 |
| transfer | 11.8805 | 16.8345 |

검토 상태 집계:

```json
{
  "transfer": {
    "accepted": 17,
    "revised_unreviewed": 7,
    "requested_revision": 7,
    "passed_after_revision_request": 6
  },
  "core": {
    "accepted": 12,
    "revised_unreviewed": 12,
    "requested_revision": 12,
    "passed_after_revision_request": 11,
    "not_reached": 12
  }
}
```

수정 요청 후 통과 수는 비판자가 없었으면 반드시 실패했을 것이라는 뜻이 아니다. 최초 계획 생성도 확률적이므로 독립 실행한 두 조건의 차이만으로 개별 지적의 인과 효과를 증명할 수 없다. 실제 토큰을 기록했으며 과금 금액이나 무료 잔여 한도를 추정해 채우지 않았다.


## 검토 전후 계획의 별도 브라우저 확인

최종 비교가 끝난 뒤, 수정 요청을 받은 유효한 원안과 최종 계획을 같은 합성 조건의 새 브라우저에서 각각 두 번 실행했다. 추가 모델 호출은 0회다. 최초 탐색 URL만 새 합성 서버 주소로 옮겼으며 동작·입력·판정 조건은 보존했다. 정식 평가 점수를 이 결과로 교체하지 않는다.

| 사례 / 반복 | 원안 통과 | 최종 계획 통과 | 변화 |
|---|---|---|---|
| transfer/activation_normal/1 | False | True | improved |
| transfer/collection_normal/2 | True | False | harmed |
| transfer/activation_bug/1 | False | True | improved |
| transfer/activation_bug/2 | False | True | improved |
| transfer/quantity_normal/2 | False | True | improved |
| transfer/activation_normal/2 | True | True | both_pass |
| core/overlap_bug/1 | True | True | both_pass |
| core/search_normal/1 | False | True | improved |
| core/overlap_bug/3 | True | True | both_pass |
| core/overlap_normal/3 | True | True | both_pass |
| core/search_bug/2 | False | True | improved |
| core/overlap_normal/1 | True | True | both_pass |
| core/search_bug/1 | False | False | both_fail |
| core/clipping_normal/2 | True | True | both_pass |
| core/clipping_bug/3 | True | True | both_pass |
| core/overlap_bug/2 | False | True | improved |

```json
{
  "model_calls": 0,
  "review_revision_requests": 19,
  "counts": {
    "improved": 7,
    "harmed": 1,
    "not_comparable_invalid_or_missing_plan": 3,
    "both_pass": 7,
    "both_fail": 1
  },
  "all_final_replays_agree": true,
  "limitation": "Post-hoc deterministic fixture replay of valid initial and final plans, not a held-out score or self-review control."
}
```

19건의 수정 요청 중 유효한 계획끼리 비교할 수 있는 16건에서 7건은 실패→통과, 1건은 통과→실패였다. 7건은 양쪽 모두 통과, 1건은 양쪽 모두 실패했다. 악화된 사례는 `collection_normal` 2회차로, 비판자가 제보에 명시된 완료 문구를 잘못 부정했고 작성자가 이를 따라 바꿨다.

원안이 호스트 검증을 통과하지 못했거나 최종 계획이 없으면 비교 불가로 남겼다. 이 점검은 같은 합성 환경에서 수정된 계획의 효과를 확인하며, 다른 모델을 쓴 효과와 추가 호출 자체의 효과를 분리하지는 않는다.

## 개발 진단도 보존

| 실행 ID | 최초 작업 통과 | 모델 호출 |
|---|---:|---:|
| `20261105T013050Z_486295af` | 0/3 | 12 |
| `20261105T013450Z_5efb413d` | 2/3 | 16 |
| `20261105T013926Z_e3ca6ecb` | 1/3 | 13 |
| `20261105T014147Z_221a883c` | 2/3 | 14 |

위 예비 실행에서 발견한 응답 모순, 잘못된 클릭, 초기 이동 누락, 생성·수정 형식 불일치를 개선했다. 예비 실행은 최종 점수에서 분리하고 증거 ZIP에는 실패를 포함해 모두 보관한다. 별도 Groq 연결 확인 2회도 기록했다. 첫 연결 확인은 사용량을 저장하지 못했으므로 0토큰으로 취급하지 않는다.

## 해석 범위

- 공개 개발 세트이며 추가 세트도 이전 실패를 보고 개선에 사용했다. 미공개 평가나 실제 고객사 정확도가 아니다.
- 3회 반복은 3개의 독립적인 새 문제를 뜻하지 않는다. 오류·보류도 분모에 포함한다.
- 비판자가 부정확한 지적을 할 수 있다. 기본값은 비활성이고, 필요한 프로그램에서 명시적으로 켠다.
- `self` 자체 검토 옵션은 구현했지만 이번 최종 비교에서는 실행하지 않았다. 추가 호출의 효과와 다른 모델을 쓰는 효과를 분리하지 못한다.
- 원인 가설·해결 방안의 정확성과 추가 질문의 유용성은 이 자동 점수에 포함되지 않는다.
- 최종 수정본을 비판자가 다시 승인했다고 표시하지 않는다. `revised_unreviewed`와 검토/최종 계획 해시를 확인한다.

## 원본 실행 ID

- `core/none`: `20261105T014547Z_c5e40b56`
- `transfer/groq`: `20261105T015440Z_465edd9b_replay_recovery`
- `transfer/none`: `20261105T020546Z_df3f5bbb`
- `core/groq`: `20261105T021418Z_1f7d9e1f`

연동과 실행 명령은 저장소의 `docs/critic-review-proposal.md`, `docs/qa-evaluation.md`를 참고한다.
