# 등록된 테스트 계정 로그인 — 0.4.0

**0.4.1 보안 변경:** 로그인 대상 원점은 유효한 인증서를 사용하는 HTTPS여야 합니다. HTTP 설정은 초기 검증에서 `PolicyError("CREDENTIALS_REQUIRE_HTTPS")`로 거절하므로 백엔드는 생성자 호출 단계에서도 이 코드를 처리해야 합니다. `session_headers`도 같은 제약을 적용하며 localhost/사설망 예외와 인증서 검증 해제 옵션은 없습니다. 인증 없는 HTTP QA만 허용합니다.

관련 이슈: [Core_engine #11](https://github.com/team-METAPHOR/Core_engine/issues/11), [백엔드 #61](https://github.com/team-METAPHOR/Metaphor-back/issues/61).

백엔드 워커가 복호화한 계정을 `LoginOptions`로 전달하면 엔진이 로그인 후 QA를 실행합니다. 탐색과 두 번의 재현은 각각 새 브라우저 컨텍스트를 사용하며, 매번 초기화와 로그인을 거칩니다. 고정 계획 재검증도 같은 과정을 사용합니다. AI가 계정이나 로그인 절차를 생성하지 않습니다.

## 워커 연결

```python
from core_engine import BrowserOptions, LoginOptions, QAService

# 이 값은 신뢰된 워커에서만 복호화합니다. 요청 본문이나 설정 객체를 로그에 남기지 않습니다.
login = LoginOptions(
    login_path="/app/login",
    login_id=decrypted_account.login_id,
    password=decrypted_account.password,
    submit_path="/app/auth",       # 생략하면 login_path로 제출
    success_path="/app/home",      # 또는 success_selector="#signed-in"
)
options = BrowserOptions(
    test_context_id="program-demo-member-v1",  # 계정·역할·데이터 조건을 식별하는 비밀 없는 ID
    login=login,
    record_video=True,
)
service = QAService(model, policy, store, options=options)
# 기존 run / retest / reset / is_authorized 인터페이스는 동일합니다.
```

`login_path`, `submit_path`, `success_path`는 승인된 원점 기준 `/`로 시작하는 경로입니다. 절대 URL, 쿼리, 범위 밖 경로를 받지 않습니다. 로그인·제출·성공 경로와 필요한 정적 자원도 프로그램의 승인 범위에 포함해야 합니다. 별도 로그인 API 경로를 사용하면 `submit_path`를 지정하세요.

| 옵션 | 동작 |
|---|---|
| `login_id`, `password` | 워커가 메모리에서 전달하는 필수 값. 모델·계획·결과에 포함하지 않음 |
| `success_path`, `success_selector` | 하나 이상 필수. 둘 다 지정하면 두 조건 모두 확인. 로그인 페이지에 이미 있는 요소는 성공 신호로 허용하지 않음 |
| `username_selector` | 기본값: `input[autocomplete="username"],input[type="email"],input[name="username"]` |
| `password_selector` | 기본값: `input[type="password"]` |
| `submit_selector` | 기본값: `button[type="submit"],button:not([type]),input[type="submit"]` |
| `timeout_ms` | 로그인 요소·탐색 대기 상한, 기본 10,000ms, 허용 100~15,000ms. 작업 전체 예산도 적용 |

폼 선택자는 신뢰된 관리자/프로그램 설정에서 필요할 때 덮어씁니다. 각 선택자가 보이고 활성화된 요소 하나를 가리켜야 합니다. 고객이 계정과 로그인 경로만 등록했다면 백엔드가 **성공 경로 또는 성공 요소를 추가로 확보**해야 합니다. 단순히 클릭됐다는 이유로 로그인 성공을 추정하지 않습니다.

## 실패와 재검증

- 잘못된 계정, 폼 불일치, 성공 신호 미확인은 `LOGIN_FAILED`입니다. 재현 시도의 단계는 `login`, 판정은 `inconclusive`이며 한국어 진단과 점검 항목을 제공합니다. 초기 탐색에서 실패하면 QA 결과의 `error_code`로 반환합니다.
- 취소·승인 철회·요청/시간 예산 초과 코드는 유지합니다. 범위 밖 호스트 설정은 실행 전에 거절합니다.
- 결과 `environment.session_mode`는 `form_login`입니다. `login_profile_sha256`는 비밀을 제외한 경로·선택자 설정의 해시입니다. 재검증 시 이 설정이나 `test_context_id`가 다르면 `BASELINE_ENVIRONMENT_MISMATCH`로 보류합니다.
- 계정의 실제 역할/데이터 변경은 백엔드가 관리하고 `test_context_id`를 변경해야 합니다. 비밀번호 자체를 해시해 결과에 남기지 않습니다.

## 기록과 네트워크

로그인 화면에서는 스크린샷·영상·콘솔·요청 증거를 수집하지 않습니다. 성공 확인 후 마스킹된 화면 기록을 시작합니다. 등록 계정 문자열이 이후 DOM 텍스트에 반영되면 요소 텍스트를 정제하고 해당 화면 요소를 마스킹합니다. 이미지/캔버스 속 문자열과 사용자가 올린 원본 스크린샷까지 자동 인식하는 기능은 아니므로 민감 영역은 기존 `mask_selectors`와 업로드 마스킹을 적용하세요.

로그인 단계에서 등록된 제출 경로로 POST 한 번을 허용합니다. URL 인코딩 폼/JSON 본문만 최대 16 KiB로 전달하며 원점·경로·DNS/IP·취소·승인·예산 검사는 유지합니다. 쿠키와 인증 헤더는 현재 컨텍스트에서만 사용합니다. 일반 QA POST는 계속 차단합니다. 301/302/303 폼 제출 응답은 승인된 GET 경로로 이동하고, 본문을 재전송하는 307/308은 거절합니다.

현재 지원 범위는 같은 원점의 단일 폼 로그인입니다. MFA·CAPTCHA·외부 SSO·팝업·다단계 로그인·자동 세션 갱신은 지원하지 않습니다. `session_headers`와 `login`을 함께 지정할 수 없습니다.

## 내보낸 Playwright 테스트

생성된 소스에는 비밀을 제외한 로그인 폼 설정과 `QA_LOGIN_ID`, `QA_LOGIN_PASSWORD` 환경변수 이름만 들어갑니다. 실행 환경의 비밀 저장소에서 두 값을 주입하세요. 기존 `QA_RESET_MODULE`, 쿼리가 있는 경우의 `QA_ENTRY_URL` 등도 그대로 필요합니다.

로그인 단계가 원본 캡처에 남지 않도록 생성 테스트의 Playwright trace/video/screenshot은 꺼져 있습니다. 플랫폼 재현 영상은 엔진이 별도로 만드는 마스킹 영상입니다. 내보낸 테스트는 엔진의 DNS/IP 고정 전송을 구현하지 않으므로 승인된 격리 테스트 환경에서 실행합니다.

내보낸 인증 테스트에서 `QA_ENTRY_URL`을 HTTP로 바꾸면 실행 전에 거절합니다. 등록된 로그인 POST의 명시적인 이동 외에 일반 응답의 리다이렉트는 인증 정보가 후속 요청으로 전달되지 않도록 차단합니다.

## 로컬 검증

`tests/test_login.py`는 외부 AI API 없이 합성 서버와 실제 브라우저를 사용합니다. 새 세션별 로그인, 잘못된 계정, 범위 차단과 일반 POST 차단, 영상 제외/연속성, 반영된 비밀번호 마스킹, 모델·결과·파일의 계정 비노출, 고정 계획 재검증, 내보낸 테스트의 결함 실패/수정 통과를 확인합니다. 이 결과는 AI 정확도나 고객 사이트 전체 호환성 수치가 아닙니다.

인증 관련 합성 서버는 임시 TLS 인증서를 생성합니다(OpenSSL 필요). Python/Node의 기존 신뢰 루트에 해당 인증서를 테스트 동안만 추가하고, 테스트용 Chromium은 해당 인증서의 공개키만 고정합니다. 시스템 인증서 저장소와 운영 엔진의 검증 설정은 바꾸지 않습니다.
