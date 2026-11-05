# 브라우저 요청과 승인된 상태 변경 — 0.6.0

관련 이슈: #15, #16, #17, #18. 기본 실행은 읽기 전용이며, 승인된 테스트 데이터에 대한 쓰기는 별도 호스트 설정으로만 켭니다. 모델·신고 JSON·페이지 내용으로 권한을 늘릴 수 없습니다.

## 페이지 로딩

- 브라우저 요청이 동시 연결 한도에 도달하면 자리가 날 때까지 기다립니다. 기본 동시 2개, 상한 4개는 그대로입니다.
- 대기 중에도 취소·권한 철회·작업 기한을 확인합니다. 실제로 보내는 요청의 분당/전체 수와 응답/누적 바이트 한도는 유지합니다.
- `BrowserOptions.navigation_timeout_ms`는 기본 15,000ms, 최대 30,000ms입니다. 일반 클릭/검사 제한과 분리됩니다.
- `BudgetLimits.request_timeout_seconds`는 연결/응답 읽기 대기의 기본 5초, 최대 30초입니다. 남은 작업 시간보다 길게 기다리지 않습니다. 전체 기한은 별도로 유지합니다.
- BugLab처럼 정적 파일과 API가 많은 페이지는 신뢰된 호스트가 예산을 조정해야 합니다. 예: `BudgetLimits(max_requests_per_minute=120, max_total_requests=300)`. 동시 연결을 무제한으로 늘리는 설정은 없습니다.

## 외부 SDK·리소스

문서 이동 이외의 요청이 `ORIGIN_OUT_OF_SCOPE` 또는 `PATH_OUT_OF_SCOPE`이면 실제 범위 밖 연결은 차단하고 브라우저 검증을 계속합니다. 문서 이동, 명시적 제외 경로, 미승인 메서드, DNS/IP 제한, 예산 초과는 계속 중단 사유입니다. 로그인 페이지의 외부 SDK도 같은 규칙으로 처리합니다.

`run.evidence[].blocked_requests`에 정제된 원요청 URL, 메서드, 리소스 종류, 차단 코드, `send_attempted`, `non_fatal`을 기록합니다. `send_attempted=true`인 리다이렉트 차단은 원래 범위 안의 첫 요청만 전송했음을 뜻하며 차단된 후속 주소로 연결하지 않습니다. 차단 기록도 작업 요청 한도로 제한합니다.

관찰 스냅샷 `network`와 Markdown 보고서에도 차단 내용을 남기고, 진단에는 `REVIEW_BLOCKED_SUBRESOURCES` 검토 조치를 추가합니다. 차단된 라이브러리·폰트가 화면을 바꿀 수 있으므로 그 상태의 재현 판정이 실제 고객 환경과 같은지는 검토해야 합니다. 필요한 스크립트 누락으로 페이지 오류나 완료 조건 실패가 나면 여전히 판정을 보류합니다. 외부 origin을 자동 허용하지 않습니다.

## 하위 요청의 HTTP 응답

일반 화면의 문서가 아닌 하위 요청 4xx는 네트워크 관찰 기록에 `non_fatal=true`로 남기고 계속합니다. 비로그인 세션 조회 401, 쿠폰 중복 409 등이 증상 자체를 확인하기도 전에 실행을 멈추지 않도록 합니다. 이것만으로 버그라고 판정하지 않으며 기존 기대/증상 검사로 판단합니다. 문서 4xx/5xx와 예상하지 않은 하위 요청 5xx는 중단합니다. 신뢰된 로그인 과정은 아래의 정확한 401 예외 경로 외에는 오류를 엄격하게 처리합니다.

## 상태 변경 승인과 초기화

```python
from core_engine import RunPolicy, StateChangePolicy, StateReset, BudgetLimits

policy = RunPolicy(
    program_id="buglab", scope_revision=2,
    base_url="https://your-approved-staging.example",
    allowed_path_prefixes=("/",),
    excluded_path_prefixes=("/api/test", "/api/auth/signup"),
    state_changes=StateChangePolicy(
        environment="staging",       # test 또는 staging; production 불가
        reset_adapter_id="buglab-v1",
        allowed_requests=(
            ("POST", "/api/cart/items"),
            ("PATCH", "/api/cart/items"),
            ("POST", "/api/cart/coupons"),
            ("POST", "/api/favorites"),
            ("DELETE", "/api/favorites"),
        ),
    ),
)

async def reset_server_state(attempt: int) -> bool:
    # 호스트가 등록한 대상/계정만 초기화. 토큰은 워커 비밀 저장소에서 읽습니다.
    # BugLab: POST /api/test/reset + X-Test-Token + {"email": 테스트 계정}
    # 실제 200 및 초기화 성공을 확인한 경우에만 True를 반환하세요.
    return await trusted_buglab_adapter.reset_account()

reset = StateReset("buglab-v1", reset_server_state)
result = await service.run(request, reset=reset, is_authorized=check_authorization)
```

`service`는 위 `policy`와 테스트 계정 `BrowserOptions`로 만든 `QAService`입니다.

- 승인 범위는 메서드와 **경로 세그먼트 접두사**의 조합입니다. `/api/cart/items`는 그 경로와 `/api/cart/items/2`를 포함하지만 `/api/cart/items-extra`는 포함하지 않습니다. 규칙 최대 32개, 쓰기 전체 `/` 허용은 거절합니다. 일반 원점·허용 경로·제외 경로 제한도 함께 적용됩니다.
- `allowed_methods=("GET", "POST")`만으로 쓰기를 켤 수 없습니다. `StateChangePolicy`가 없으면 POST/PUT/PATCH/DELETE는 계속 `METHOD_NOT_ALLOWED`입니다. 등록된 로그인 POST 예외는 기존대로 유지합니다.
- `StateReset.adapter_id`가 승인 설정과 정확히 같아야 합니다. 일반 콜백이나 `stateless` 설정으로 쓰기를 켤 수 없습니다. 불일치하면 네트워크 실행 전에 `STATE_RESET_REQUIRED`입니다.
- 초기 화면 탐색(0)과 독립 시도(1, 2), 재검증에서도 초기화 → 새 브라우저 컨텍스트 → 새 로그인 순서입니다. 실패하면 `RESET_FAILED`이며 테스트 쓰기를 보내지 않습니다.
- `ConditionsService`에는 `StateReset("buglab-v1", reset_condition_state)`를 넘기며 콜백은 `(condition_id, attempt)`를 받습니다. 각 조건의 계정/서버 데이터를 실제로 초기화해야 합니다. 일반 QA/재검증 콜백은 `(attempt)`입니다.
- 엔진은 호스트가 제공한 초기화 구현이 실제 DB를 비우는지 스스로 증명하지 않습니다. 어댑터는 계정별 장바구니·쿠폰·찜·세션까지 지우고 성공 여부를 검증해야 합니다. 병렬 작업은 계정/테스트 데이터를 분리하거나 잠금을 사용해야 합니다.
- 요청 본문은 JSON/URL 인코딩 폼만 최대 16KiB입니다. 현재 컨텍스트의 Cookie와 제한된 인증/CSRF 헤더를 전달합니다. 인증 정보는 HTTPS에서만 전송합니다. 파일 업로드는 지원하지 않습니다.
- 상태 변경 응답의 리다이렉트는 후속 전송 없이 `STATE_CHANGE_REDIRECT_UNSUPPORTED`로 중단합니다. 쓰기 본문을 다른 주소에 다시 보내지 않습니다.
- `run.evidence[].state_changing_requests`는 메서드·경로·전송 시도 여부·응답 코드·차단 코드만 기록합니다. 본문·쿠키·인증 헤더를 기록하지 않습니다. 응답 실패 시에도 이미 보낸 쓰기 시도를 숨기지 않습니다.
- 승인 내용은 `report.environment.state_changes`에 저장하고 재검증 때 비교합니다. 다른 승인으로 바뀌면 `BASELINE_ENVIRONMENT_MISMATCH`입니다. 새 설정으로 기준 재현을 다시 만드세요.

## SPA 로그인

로그인 폼이 비동기로 렌더링되는 페이지는 등록된 제한 시간 안에서 폼이 표시되고 활성화될 때까지 기다립니다.

로그인 전 `GET /api/auth/me`의 401이 정상인 앱은 다음처럼 호스트에서 명시합니다.

```python
login = LoginOptions(
    # 기존 로그인 경로·계정·선택자·성공 확인 설정
    ...,
    unauthenticated_paths=("/api/auth/me",),
)
```

이 예외는 **로그인 중, 정확히 등록된 경로의 GET fetch/XHR, 401 응답**에만 적용됩니다. 로그인 POST 실패, 403/500, 문서 401, 미등록 경로와 로그인 이후의 401은 면제하지 않습니다. 예외 경로도 승인된 원점·범위 안이어야 합니다.

## 내보낸 Playwright 테스트

기존 `QA_RESET_MODULE` 모듈에 `export const adapterId = "buglab-v1"`를 추가하고, 명시적으로 `QA_ALLOW_STATE_CHANGES=buglab-v1`을 설정해야 쓰기 테스트가 동작합니다. 내보내기에 사용한 승인 원점은 변경할 수 없습니다. 모듈의 `reset(attempt)`가 매번 실제 서버 초기화 성공 시에만 `true`를 반환해야 합니다.

호스트의 수동 승인을 요구하는 환경변수이며 사용자/모델 입력으로 자동 설정하지 마세요. 내보낸 테스트는 엔진의 DNS/IP 고정 전송을 구현하지 않으므로 신뢰된 격리 테스트 환경에서만 실행합니다.

## 확인한 범위

`tests/test_request_scheduling.py`: 8개 정적 파일/동시 2개, 큐 취소·철회·기한, 차단된 외부 SDK/CSS/fetch, 쓰기 기본 거부·초기화 강제·JSON/쿠키/CSRF 전달·증거 기록을 검증합니다. 기존 TLS/로그인/증거/라이브/내보내기 테스트도 유지합니다.

실제 BugLab 연동 테스트는 빌드한 격리 체크아웃을 지정해 실행합니다. 기존 `.env`와 DB는 사용하지 않으며 임시 DB·계정·TLS 프록시를 생성하고 종료합니다.

```sh
PYTHONPATH=src:tests BUGLAB_CHECKOUT=/absolute/path/to/built/BugLab-shop \
  python -m unittest test_buglab_integration -v
```

BugLab `dd574f1577d86ef4b3a157038fa3ad87c49b80e7`의 B02를 고정 시나리오로 두 독립 시도에서 확인했습니다. 수량 2→1 뒤 전체 상품 합계가 36,000원에 남는 증상입니다. 이는 엔진 연동 회귀 확인이며 AI 자동 계획 정확도, 전체 6개 시나리오 지원, 운영 배포 완료를 뜻하지 않습니다. 현재 계획 어휘에 브라우저 back/reload 전용 동작은 포함되지 않습니다.

## 백엔드 적용 체크

새 0.6.0 wheel/커밋을 워커까지 반영하고, 관리자 설정·scope revision·실제 초기화 어댑터·TLS·로그인 전 401 허용 경로를 연결해야 합니다. 운영 DB에 영향을 주는 프로그램에는 켜지 마세요. 엔진 머지만으로 기존 배포가 자동 변경되지는 않습니다. 보고서/상세 API는 새 차단 목록·쓰기 감사 목록·진단 조치를 보존해야 합니다.
