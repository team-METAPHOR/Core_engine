# 첫 실제 AI 평가 결과 — 2026-11-05

공개 합성 개발용 12개 사례를 각 3회 실행한 최초 측정이다. 실행 도중 프롬프트나 엔진을 바꾸지 않았다. 실제 고객사 환경의 정확도 또는 미공개 평가 결과로 일반화할 수 없다.

- 실행 ID: `20261104T235308Z_6e4680b9` (디렉터리 시각은 UTC).
- 엔진 커밋: `fbdaa85061809a88f0e2b0fe3073bbdfdc8f7374`; 소스 수정 상태: `False`.
- 세트: `qa-small-v1`; 실행 순서 seed: `17`.
- 모델: `gpt-5.4-mini-2026-03-17`.
- 최초 작업: 36/36건 완료, 자동 채점 13건 통과.
- 평가기 자체 오류: 0건. 전체 공급자 호출 상한: 120회.

## 결과

| 지표 | 건수 | 비율 |
|---|---:|---:|
| 제보 입력·절차까지 일치한 버그 재현 | 1/12 | 8.3% |
| 제보 입력·절차까지 일치한 정상 판정 | 3/12 | 25.0% |
| 정상 대상 오탐 | 0/12 | 0.0% |
| 버그/정상 대상 판정 완료 | 7/24 | 29.2% |
| 정보 부족 시 질문 분기 | 3/6 | 50.0% |
| 환경 장애 구분 | 6/6 | 100.0% |
| 동일 계획 수정 확인 | 1/12 | 8.3% |

정상 대상 오탐률만으로 성능을 판단하지 않는다. 판정 보류·계획 실패 때문에 오탐이 없을 수도 있으므로 정상 판정 성공률과 판정 완료율을 함께 본다.

## 사례별 반복 결과

| 사례 | 통과 / 반복 | 실패 사유 |
|---|---:|---|
| `search_bug` | 1/3 | INDEPENDENT_ORACLE_FAILED: 2회 |
| `search_normal` | 2/3 | INDEPENDENT_ORACLE_FAILED: 1회 |
| `filter_bug` | 0/3 | VERDICT_MISMATCH: 3회 |
| `filter_normal` | 1/3 | VERDICT_MISMATCH: 2회 |
| `clipping_bug` | 0/3 | INVALID_PLAN_OR_BASELINE: 3회 |
| `clipping_normal` | 0/3 | INVALID_PLAN_OR_BASELINE: 3회 |
| `overlap_bug` | 0/3 | INVALID_PLAN_OR_BASELINE: 3회 |
| `overlap_normal` | 0/3 | INVALID_PLAN_OR_BASELINE: 3회 |
| `missing_expectation` | 2/3 | MISSING_INFORMATION_NOT_HANDLED: 1회 |
| `missing_search_data` | 1/3 | MISSING_INFORMATION_NOT_HANDLED: 2회 |
| `environment_unavailable` | 3/3 | 없음 |
| `environment_unauthorized` | 3/3 | 없음 |

**독립 검사 미통과의 해석:** `search_normal` 2회차, `search_bug` 2회차, `search_bug` 3회차에서는 기대한 화면 상태는 관찰되었지만 지정된 입력·조작 일치 조건을 충족하지 않았다. 실제 화면 증상을 관찰하지 못한 실패와 구분해야 한다. 정확한 조작 기록은 원본 `results.json`의 `oracle.attempts[].actions`에 남아 있다.

## 사용량과 시간

- 공급자 호출: 80회.
- 보고된 입력/출력 토큰: 268,728 / 35,749.
- 사용량 미보고 호출: 0회.
- 최초 QA 작업 실행 시간 중앙값: 7.642초. 이미지 준비와 독립 재실행은 제외한다.
- 금액은 추정하지 않았다. 토큰 기록만으로 캐시 등 실제 청구 조건을 확인할 수 없다.

## 해석과 다음 평가

- 시각 검사나 정보 부족 처리에서 실패가 있었다면 해당 엔진 단계의 개선 대상으로 삼되, 이 최초 결과를 교체하거나 삭제하지 않는다.
- `INVALID_PLAN_OR_BASELINE`는 이 신규 작업에서 생성 계획 검증이 실패했다는 코드다. 세부 원인을 확인하지 않고 특정 모델 행동을 원인으로 단정하지 않는다.
- 질문 분기 통과는 적절한 질문 내용까지 입증하지 않는다. 질문과 해결 방안의 품질은 별도 사람 평가가 필요하다.
- 버그/정상 쌍은 같은 과거 제보를 각각 버그 구현과 정상 구현에 전달한다. 정상 스크린샷 자체의 분류 정확도를 측정한 결과는 아니다.
- 이 버전의 기능 사례는 제보에 명시된 입력값과 마지막 조작 순서까지 일치해야 통과한다. 예를 들어 `없는상품` 대신 다른 검색어를 사용하면 같은 증상이 관찰되어도 미통과다. 따라서 점수는 제보 절차를 그대로 재현하는 능력으로 해석한다.
- 수정 확인 분모에는 기준 재현에 실패하여 재검증하지 못한 항목도 포함한다.
- 반복 횟수는 서로 다른 문제 수가 아니다. 다음 성능 발표에는 별도로 만든 미공개 사례와 고정된 평가 기준을 사용한다.

## 재실행

```bash
PYTHONPATH=src python -m core_engine.evaluation --live --repeats 3 --retest-fixed --seed 17 --max-model-calls 120
```

모델 응답과 실행 시간은 반복 시 달라질 수 있다. 자세한 채점 방법은 [평가 실행 명세](qa-evaluation.md)를 참고한다.

엔진 Python 소스 SHA-256: `7039cf432db782afef32e71e2febe40e83d2ce985dda7624b6fb834ee30b87f8`.
