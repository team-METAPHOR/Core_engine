# Core_engine

team METAPHOR의 웹 버그·보안 취약점 제보 검증 엔진입니다. 공개형 버그 바운티와 기업 간 의뢰에서 접수한 제보를 승인된 환경에서 재현하고, 근거와 분석 결과를 담당자에게 제공하는 것을 목표로 합니다. 제보자가 개인인지 기업인지는 엔진 실행 조건에 영향을 주지 않습니다.

현재는 **승인 범위 검사와 실제 Chromium 재현 실행**까지 구현했습니다. AI 분류·심각도 평가·해결 방안 생성은 이후 단계입니다.

## 구현된 기능

| 모듈 | 기능 |
|---|---|
| `policy.py` | 원점·경로·메서드·DNS/IP 검사, 제외 경로 우선 적용, 리다이렉트 검사 |
| `budget.py` | 작업 전체의 요청 수·분당 요청·동시 요청·실행 시간·응답 용량 제한 |
| `transport.py` | 승인된 숫자 IP에 직접 연결, 원래 호스트의 HTTP Host·TLS 인증서 검사 유지 |
| `plan.py` | 페이지 이동·클릭·입력·텍스트 확인 동작을 최대 20단계로 제한 |
| `browser.py` | Playwright 실행, 시도별 새 브라우저 컨텍스트, 초기화 확인, 취소·승인 철회 확인 |
| `verdict.py` | 두 시도의 근거를 비교해 재현됨·재현 안 됨·판단 불가 판정 |

브라우저의 HTTP 요청은 모두 가로채 전용 전송 모듈로 처리합니다. 이 모듈은 요청마다 DNS 결과 전체를 검사하고, 검사한 IP에 연결합니다. 브라우저가 원래 호스트로 직접 요청을 보내게 하지 않습니다. 현재는 한 작업에 하나의 정확한 원점만 지원하며 허용 메서드는 `GET`, `HEAD`, `OPTIONS`입니다.

## 설치

Python 3.11 이상이 필요합니다. 브라우저 실행에는 Playwright와 Chromium이 필요합니다. 아래 명령은 macOS/Linux 기준입니다.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install ".[browser]"
python -m playwright install chromium --only-shell
```

Linux에서 브라우저 실행에 필요한 시스템 라이브러리가 없다면 `python -m playwright install --with-deps chromium --only-shell`을 사용합니다. 정책·예산·판정 모듈만 사용하려면 `python -m pip install .`로 설치할 수 있습니다. 소스를 수정한 뒤에는 다시 설치합니다.

## API 키 없이 로컬 데모 실행

```bash
python examples/demo.py
python examples/demo.py --fixed
```

첫 번째 명령은 검색 결과가 비었을 때 이전 목록이 남는 예제를 두 번 실행해 `reproduced`를 출력합니다. 두 번째는 수정된 예제를 실행해 `not_reproduced`를 출력합니다. 예제 서버는 `127.0.0.1`의 임시 포트에서 실행되고 종료 시 정리됩니다. 외부 사이트나 AI API에 접속하지 않습니다.

## 실행 계획과 결과

`BrowserPlan`은 다음 입력을 받습니다.

- `steps`: `navigate`, `click`, `fill`, `assert_text`로 구성된 불변 목록. 첫 단계는 페이지 이동이어야 합니다.
- `ready`: 동작 완료를 확인하는 CSS 선택자와 텍스트. 로딩 중인 화면을 완료된 결과로 판정하지 않도록 반드시 지정합니다.
- `symptom`: 신고된 증상을 확인할 CSS 선택자와 텍스트. 현재는 화면에 존재하는 요소의 `inner_text()`를 읽고 양끝 공백을 제거해 비교합니다.
- `step_timeout_ms`: 각 동작·확인의 제한 시간. 기본 3초, 최대 5초입니다.

`BrowserRunner.run()`에는 계획 외에 신뢰할 수 있는 서버 코드로 작성한 두 콜백이 필요합니다.

- `reset(attempt)`: 각 시도 전에 테스트 서버 상태를 초기화하고 성공 시 `True`를 반환하는 비동기 함수. 새 브라우저를 만드는 것만으로 서버 상태가 초기화되지는 않습니다.
- `is_authorized()`: 현재 프로그램 승인 버전과 중지 상태를 확인하는 빠른 동기 함수. `True`가 아니거나 확인에 실패하면 후속 요청을 차단합니다. 계획이나 사용자 입력에서 이 함수를 생성하면 안 됩니다.

선택적으로 `threading.Event`를 `cancelled`에 전달할 수 있습니다. 이벤트가 설정되면 후속 요청과 단계를 중단합니다. 이미 전송한 요청은 회수할 수 없습니다.

결과 `BrowserRun`에는 프로그램 ID, 범위 버전, 계획 해시, 시도별 결과, 최종 판정, 실행 요약 근거가 포함됩니다.

| 판정 | 조건 |
|---|---|
| `reproduced` | 두 시도 모두 정상 완료되고 같은 증상 텍스트가 확인됨 |
| `not_reproduced` | 두 시도 모두 정상 완료됐으나 증상 텍스트가 확인되지 않음 |
| `inconclusive` | 시도 간 불일치, 초기화 실패, 선택자·준비 상태 실패, 정책 차단, 한도 초과 등 |

`not_reproduced`는 버그가 없다는 확정 판정이 아닙니다. 증상 요소가 없거나 페이지 동작 자체가 실패하면 `inconclusive`입니다. 잘못된 계획은 브라우저를 시작하기 전에 예외로 거절하고, Playwright 설치·브라우저 시작 같은 실행 환경 오류는 호출자에게 예외로 전달합니다.

근거에는 시각, 시도 번호, 완료 단계 수, 준비 상태, 증상 확인 여부, 차단 코드만 포함합니다. 입력한 값·DOM 원문·쿠키를 결과에 복사하지 않습니다. **스크린샷·전체 DOM 수집, 마스킹 저장소, 영속 저장은 아직 구현 전**입니다. 현재 반환된 실행 요약은 호출자가 저장해야 합니다.

## 테스트

설치한 가상 환경에서 실행합니다.

```bash
python -m unittest discover -s tests -v
```

검사는 로컬 테스트 서버를 사용합니다. 실제 Chromium으로 정상 재현·수정 확인·시도 간 불일치·초기화 실패·요청 차단을 확인하고, 전송 모듈의 IP 고정·원래 Host 유지·승인 철회·용량 제한을 검사합니다. Playwright가 없으면 브라우저 검사는 건너뛰므로, 전체 검증에는 `.[browser]` 설치와 Chromium 준비가 필요합니다.

## 현재 지원 범위와 배포 조건

- 고객사가 승인한 스테이징·합성 테스트 환경의 익명 세션만 지원합니다. 로그인 상태·쿠키·인증 헤더는 전달하지 않습니다.
- POST, WebSocket, 지속적인 SSE, 압축 응답, 다운로드·팝업은 지원하지 않습니다. 정책 차단·HTTP 오류·페이지 스크립트 오류가 생기면 결과를 보류합니다.
- 서비스 워커를 차단하고 브라우저의 직접 HTTP 접속에 실패하는 프록시를 설정합니다. 이것은 운영체제 수준의 네트워크 방화벽을 대신하지 않습니다.
- 실제 외부 대상에 배포하기 전에는 별도 워커 프로세스/컨테이너, 네트워크 출구 제한, 메모리 제한, 강제 종료 감독이 필요합니다. DNS 조회·운영체제 호출까지 엄격한 종료 시간을 보장하려면 외부 프로세스 감독이 필요합니다.
- GET만 허용해도 서버의 상태 변경이 완전히 방지되지는 않습니다. 고객사는 테스트 대상과 제외 경로를 좁게 승인해야 합니다. 삭제·결제·로그아웃 등 알려진 경로는 추가 차단합니다.

## 백엔드와의 역할 분담

엔진은 정책 검사·실행·판정과 결과 생성을 담당합니다. 고객사 권한, 승인된 범위 버전 발급, 작업 큐, 결과 저장, 조회 API와 관제 화면은 백엔드·프론트의 책임입니다. `RunPolicy`는 신뢰할 수 있는 백엔드의 승인된 범위로 생성해야 하며, 제보자나 AI가 보낸 허용 범위를 그대로 사용하면 안 됩니다.

## 이후 개발 단계

1. 마스킹된 스크린샷·DOM 근거와 영속 저장 인터페이스.
2. 신고의 보안 의심·일반 버그·판단 보류 분류와 제한된 AI 재현 계획 생성.
3. 근거 기반 심각도 추정, 일반 버그와 보안 취약점 후보의 해결 방안·검증 절차 제안. 최종 검토는 사람이 수행합니다.
4. 백엔드 작업 큐·진행 이벤트 연동과 운영 워커 격리.

## 코드 출처와 참고

정책·예산·근거 기반 판정은 [IWANTGOHOME의 고정 커밋](https://github.com/Kowntaewook/IWANTGOHOME/tree/27f6835d2aaae2e5515eb19c360e2242c3308fcd)의 `programs.py`, `web.py`, `research_policy.py`, `full_hunt/scenario.py`를 바탕으로 이식했습니다. 브라우저 요청 가로채기와 숫자 IP 연결 방식은 같은 저장소의 `browser_sessions.py`를 참고했습니다.

브라우저 연동은 Playwright의 [컨텍스트 요청 가로채기](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route), [WebSocket 라우팅](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-route-web-socket), [요청 응답 제공](https://playwright.dev/python/docs/api/class-route#route-fulfill) API를 사용합니다.
