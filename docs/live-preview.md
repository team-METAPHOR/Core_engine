# 실시간 재현 화면 연동 — 엔진 0.5.0

엔진은 실행 중 **마스킹된 JPEG 화면과 진행 단계**를 작업별 메모리 버퍼에 제공합니다. 백엔드 워커가 버퍼를 읽어 중계하고 관제 페이지가 표시합니다. 완료 후 WebM 영상과 QA 보고서는 기존 경로를 사용합니다.

이 문서의 Python API와 `live-1` 메시지는 엔진에 구현되어 있습니다. **공개 WebSocket 엔드포인트, 프로세스 간 중계, 회사별 조회 권한, 관제 UI는 백엔드/프론트에서 구현해야 합니다.**

## 1. 활성화 및 수명

```python
from core_engine import LiveOptions, LiveStream

live = LiveStream(LiveOptions(fps=2, max_width=960))
result = await service.run(
    request, reset=reset, is_authorized=is_authorized,
    cancelled=cancelled, live=live,
)
```

- `live=None`이 기본값입니다. 기존 호출은 그대로 동작합니다.
- `run`, `resume`, `retest`마다 **새 LiveStream**을 전달합니다. 재사용하면 `ValueError`입니다.
- `QAService.resume(..., live=live)`, `QAService.retest(..., live=live)` 모두 지원합니다.
- `ConditionsService.run(..., live_factory=lambda condition_id: 새_스트림)`은 조건마다 독립 스트림을 사용합니다. 호스트가 `condition_id → stream → engine job_id`를 매핑합니다. 조건 선택 단계는 부모 작업의 기존 진행 정보를 사용합니다.
- `ConditionsService.retest(..., live=live)`도 지원합니다.
- `live.job_id`는 엔진 작업을 만든 뒤 설정됩니다. API의 validation job ID와 동일하다고 가정하지 마세요.
- `QAService.run`의 `live.closed`는 정상 종료, 정보 부족, 실패, 취소, 예외 모두 종료 시 참입니다. run의 승인 거부/입력 오류처럼 엔진 작업 생성 전 예외에는 메시지 없이 닫힙니다. resume/retest의 저장된 작업 조회 등 run 진입 전 실패는 호출 예외로 처리하고 소비자를 정리하세요.
- `record_video=False`이거나 ffmpeg가 없어도 미리보기는 동작합니다. 미리보기와 녹화를 함께 켜면 주기적 마스킹 화면 수집을 공유합니다.

## 2. 소비자 인터페이스와 느린 연결

`live.read(after_sequence=0)`은 즉시 `tuple[LiveMessage, ...]`를 반환합니다. 네트워크 I/O나 사용자 콜백을 실행하지 않습니다. 별도 스레드에서 읽을 수 있습니다.

- 메시지 전체의 `sequence`는 작업 내에서 단조 증가합니다. 마지막으로 처리한 번호를 다음 `read`에 전달하세요.
- **진행 메시지 최근 128개 + 최신 화면 한 장**만 보관합니다. 화면 교체와 이력 제한에 따른 번호 공백은 정상입니다. 모든 화면/이벤트의 전달 보장이 필요한 감사 로그가 아닙니다.
- 재접속은 `read(0)`으로 보관 중인 진행 기록과 최신 화면을 읽고, 영속 상태는 기존 작업 조회 API로 보완합니다.
- 새 시도가 시작되면 엔진은 이전 화면을 비웁니다. 클라이언트도 `progress.phase=attempt_started`에서 이전 화면을 비워야 합니다.
- 종료 후에는 추가 메시지를 만들지 않습니다. 취소·승인 철회·비정상 중단은 메모리의 최신 이미지도 제거합니다. 이미 전송된 이미지/중계 캐시는 백엔드와 프론트가 같은 시점에 제거해야 합니다.
- 소비자가 읽지 않아도 QA는 계속 진행합니다. 별도 소비자에서 **비동기 전송 + 타임아웃 + 최신 화면 교체**를 적용하세요. 동기 네트워크 호출로 엔진의 이벤트 루프를 막으면 안 됩니다.

독립 소비자의 최소 예시입니다. `publish`는 백엔드가 구현하는 비동기 IPC/브로커 전송 함수입니다.

```python
import asyncio
from core_engine import LiveStream

async def relay(live, publish):
    cursor = 0
    try:
        while True:
            closed = live.closed  # read 전에 확인: 전송 중 생긴 마지막 end를 놓치지 않습니다.
            for message in live.read(cursor):
                async with asyncio.timeout(1):
                    await publish(message.to_wire())
                cursor = message.sequence
            if closed:
                return
            await asyncio.sleep(0.1)
    except (TimeoutError, ConnectionError, OSError):
        # 미리보기 중계 장애만 기록하고 QA 작업은 취소하지 않습니다.
        return

live = LiveStream()
publisher = asyncio.create_task(relay(live, publish))
try:
    result = await service.run(request, reset=reset, is_authorized=is_authorized, live=live)
finally:
    # QA 예외/완료와 무관하게 소비자 정리. 실제 워커에서는 중계 예외도 별도로 관찰합니다.
    try:
        await asyncio.wait_for(publisher, timeout=2)
    except Exception:
        publisher.cancel()
        await asyncio.gather(publisher, return_exceptions=True)
```

## 3. 메시지 규격

`message.metadata()`는 이미지 바이트를 제외한 새 dict를 반환합니다. 모든 메시지는 다음 필드를 가집니다.

| 필드 | 의미 |
|---|---|
| `schema_version` | `live-1` |
| `kind` | `status`, `progress`, `frame`, `preview_warning`, `end` |
| `sequence` | 해당 엔진 작업 내 증가하는 정수 |
| `created_at` | UTC ISO 8601 생성 시각 |
| `job_id`, `program_id`, `report_id` | 엔진이 바인딩한 작업·프로그램·신고 식별자 |
| `data` | 아래 종류별 필드 |

| kind | data |
|---|---|
| `status` | `phase`, 선택적 `revision`, `step_index`. 제보 해석·계획·실행·보고서 생성 등 기존 작업 단계 |
| `progress` | `attempt`, `revision`, `phase`, `step_index`, `action`, `total_steps` |
| `frame` | 현재 progress 필드 + `mime_type=image/jpeg`, `width`, `height`, `bytes`, `masked=true` |
| `preview_warning` | `error_code`: `LIVE_FRAME_TOO_LARGE`, `LIVE_FRAME_ENCODING_FAILED`, `LIVE_CAPTURE_INTERRUPTED` |
| `end` | `status`, `error_code`. 일반 종료는 결과/보고서 파일 저장 후 전송. 저장 실패 등 예외는 `interrupted`/`LIVE_RUN_INTERRUPTED` |

`attempt=0`은 계획을 위한 최초 관찰이고 1·2는 독립 재현 시도입니다. `revision=-1`은 최초 관찰, 0 이상은 재현 계획 버전입니다. 단계 인덱스는 0부터 시작하고 해당하지 않으면 null입니다. `action`은 `navigate`, `click`, `fill`, `assert_text` 또는 null입니다. URL·선택자·입력값·신고 원문·AI 응답·로그인 정보는 메시지 메타데이터에 넣지 않습니다.

주요 `progress.phase`: `attempt_started`, `login`, `discovery`, `action`, `action_completed`, `readiness`, `outcome`, `completed`, `attempt_finished`, `attempt_failed`.

**진행 단계의 `completed`는 한 시도의 단계 완료입니다. 전체 작업 완료는 `kind=end`와 기존 작업 상태로 판단합니다.** 엔진 보고서 파일 생성 이후에도 백엔드의 DB/저장소 반영 시간이 있으므로, 프론트는 end를 받은 뒤 결과 API의 준비 상태를 확인합니다.

## 4. WebSocket 전송 규격

`message.to_wire()`가 전송할 데이터를 반환합니다.

- 상태/진행/종료: JSON 문자열 → WebSocket text message.
- 화면: `4바이트 unsigned big-endian 헤더 길이` + `UTF-8 JSON metadata` + `JPEG 바이트` → **하나의 binary message**.
- 이미지와 메타데이터를 분리된 메시지로 보내지 않습니다. sequence/시도와 화면이 엇갈리는 것을 방지합니다. Base64 변환도 필요 없습니다.

프론트 디코딩 예시:

```javascript
socket.binaryType = "arraybuffer";
function decodeFrame(buffer) {
  if (buffer.byteLength < 4) throw new Error("invalid frame");
  const size = new DataView(buffer).getUint32(0, false);
  if (size > 4096 || 4 + size >= buffer.byteLength) throw new Error("invalid header");
  const meta = JSON.parse(new TextDecoder().decode(buffer.slice(4, 4 + size)));
  const jpeg = buffer.slice(4 + size);
  if (meta.schema_version !== "live-1" || meta.kind !== "frame" ||
      meta.data.mime_type !== "image/jpeg" || meta.data.bytes !== jpeg.byteLength ||
      jpeg.byteLength > 512 * 1024) throw new Error("invalid frame payload");
  return {meta, image: new Blob([jpeg], {type: "image/jpeg"})};
}
```

표시 시 기존 Blob URL을 해제하고 새 URL을 연결합니다. 오래된 sequence, 다른 job/condition/revision/attempt의 화면을 현재 화면에 덮어쓰지 않도록 검사하세요. 재접속 시 현재 엔진 job ID가 바뀌면 커서를 초기화합니다.

## 5. 제한과 권한 경계

- 기본 2fps, 설정 가능 1~4fps. 기본 최대 가로·세로 960px, JPEG 품질 65, 프레임 최대 256KiB입니다. 실제 갱신 속도/지연을 보장하는 SLA는 아닙니다.
- 화면당 상한을 넘으면 버리고 경고를 보냅니다. JPEG 인코딩/미리보기 수집 오류가 재현 판정을 바꾸지 않습니다. 경고는 코드당 한 번만 보냅니다.
- 입력란·기존 민감 선택자·등록 로그인 비밀값 마스킹을 재사용합니다. 등록된 로그인/제출 화면은 제외하고 화면 수집 전후에 승인·범위를 검사합니다. **모든 개인정보에 대한 OCR 마스킹은 아닙니다.** 승인된 테스트 데이터와 사이트별 `mask_selectors`가 필요합니다.
- 미리보기 JPEG는 파일/영상/AI 입력으로 저장·전달하지 않습니다. 기존 QA 증거 PNG/WebM 저장은 독립적으로 유지됩니다.
- 엔진의 `LiveStream`은 인증 서버나 공개 포트가 아닙니다. 회사별 접근 통제는 백엔드 책임입니다. 기존 대상 사이트 WebSocket 차단 정책을 풀지 마세요. 관제 WebSocket은 대상 사이트 브라우저와 별개의 연결입니다.

## 6. 백엔드/프론트 연결 작업

백엔드: 워커의 run/resume/retest/조건 실행에 스트림을 전달하고, 별도 프로세스인 API 서버까지 TTL 있는 중계 경로를 연결합니다. API job ID·engine job ID·org ID·condition ID를 서버에서 매핑하고, 기존 인증/회사 권한과 동일한 정책으로 **제안 경로 `WS /api/v1/validation-jobs/{job_id}/live`**를 제공합니다. 기존 REST events API는 유지합니다. 중계 종료·권한 철회·워커 장애 때 상태와 캐시를 정리하고, 재접속·heartbeat·사용자별 연결 수 제한을 구현합니다. 이 경로는 아직 구현된 API가 아닙니다.

프론트: 신고 상세 실행 카드에 실시간 화면, 현재 작업 단계, 시도/단계 번호, 마지막 갱신 시각을 표시합니다. 연결 대기/재접속/화면 없음/전송 장애/재현 실패/완료를 구분합니다. 미리보기 끊김을 QA 실패로 표시하지 않습니다. 종료 후 기존 결과 API로 확인하고 영상/보고서로 연결합니다. 원격 클릭/키보드 제어는 제공하지 않습니다.

## 7. 검증

```bash
PYTHONPATH=src:tests python -m unittest test_live -v
```

고정 모델 응답과 실제 Chromium/합성 사이트로 완료 전 프레임 수신, 마스킹, 로그인 제외, 녹화와 동시 실행, ffmpeg 미설치, 커서/최신 프레임/메모리 상한, 권한 철회·취소, 재검증·추가 답변·조건별 격리를 검사합니다. 외부 AI API 호출이나 AI 성능 평가가 아닙니다.
