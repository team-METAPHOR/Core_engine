"""Evidence-based failure explanations, independent of model availability."""

from .redaction import redact_text


_REASONS = {
    "LOGIN_FAILED": ("environment_blocked", "등록된 테스트 계정으로 로그인하거나 로그인 성공을 확인하지 못했습니다.", "테스트 계정·로그인 경로·폼 선택자·성공 확인 조건을 점검해주세요. 추가 인증이 필요한 화면은 현재 자동 로그인으로 처리하지 않습니다."),
    "STALE_COMPLETION_SIGNAL": ("execution_blocked", "이전 동작의 완료 표시만 남아 있고 마지막 동작 이후 새 처리 증거를 확인하지 못했습니다.", "마지막 입력 뒤 필요한 제출·검색 동작이 누락됐는지 확인하고, 마지막 동작의 완료 조건을 명확히 해주세요."),
    "STEP_OR_READINESS_FAILED": ("execution_blocked", "조작 대상 또는 처리 완료 조건을 확인하지 못했습니다.", "재현 순서·선택자·완료 문구를 확인해주세요."),
    "CHECK_TARGET_UNAVAILABLE": ("execution_blocked", "결과를 확인할 화면 요소가 없거나 표시되지 않았습니다.", "권한과 화면 상태가 제보 당시 조건과 같은지 확인해주세요."),
    "AMBIGUOUS_CHECK_TARGET": ("execution_blocked", "검사 대상이 여러 개여서 결과를 하나로 결정하지 못했습니다.", "검사할 요소를 구분할 안정적인 선택자가 필요합니다."),
    "ORACLE_AMBIGUOUS": ("inconclusive", "정상 조건과 증상 조건을 명확하게 구분하지 못했습니다.", "기대 결과와 증상을 서로 구분할 수 있게 구체화해주세요."),
    "HTTP_ERROR": ("environment_blocked", "실행 중 HTTP 오류 응답이 관찰되어 판정을 보류했습니다.", "관찰된 응답 코드와 테스트 서버 상태를 확인해주세요."),
    "NAVIGATION_FAILED": ("environment_blocked", "등록된 페이지로 정상 이동하지 못했습니다.", "접속 주소·인증·테스트 서버 상태를 확인해주세요."),
    "DISCOVERY_FAILED": ("environment_blocked", "계획 작성에 필요한 초기 화면을 관찰하지 못했습니다.", "대상 페이지의 접근과 로딩 상태를 확인해주세요."),
    "BROWSER_UNAVAILABLE": ("environment_blocked", "요청한 브라우저를 시작하지 못했습니다.", "해당 Playwright 브라우저와 운영체제 의존성을 설치해주세요."),
    "METHOD_NOT_ALLOWED": ("scope_restricted", "현재 실행 범위에서 허용하지 않은 요청 방식이 필요했습니다.", "허용된 읽기 전용 테스트 환경을 준비하거나 해당 절차를 수동 검증해주세요."),
    "STATE_CHANGING_PATH": ("scope_restricted", "현재 엔진이 제한하는 상태 변경 경로에 도달했습니다.", "상태 변경이 없는 테스트 경로를 준비하거나 별도 수동 검증해주세요."),
    "PAGE_SCRIPT_ERROR": ("environment_blocked", "페이지 스크립트 오류가 관찰되어 판정을 보류했습니다.", "정제된 페이지 오류와 동일 시점의 서버 로그를 확인해주세요."),
    "AUTHORIZATION_REVOKED": ("cancelled", "작업의 실행 권한이 철회됐습니다.", "유효한 실행 권한이 확인된 뒤 새 작업을 요청해주세요."),
    "AUTHORIZATION_UNAVAILABLE": ("environment_blocked", "실행 권한을 확인하는 서비스에 문제가 생겼습니다.", "백엔드의 실행 권한 확인 기능을 점검해주세요."),
    "CANCELLED": ("cancelled", "실행이 취소됐습니다.", "필요하면 기존 증거를 검토한 뒤 다시 요청해주세요."),
    "RESET_FAILED": ("environment_blocked", "독립 실행을 위한 테스트 상태 초기화에 실패했습니다.", "초기화 콜백과 테스트 데이터 상태를 확인해주세요."),
    "MODEL_RATE_LIMITED": ("model_unavailable", "AI 공급자의 사용량 제한으로 중단됐습니다.", "공급자 한도를 확인한 뒤 새 작업으로 재시도해주세요."),
    "MODEL_AUTH_FAILED": ("model_unavailable", "AI 공급자 인증에 실패했습니다.", "서버의 비밀정보 설정과 공급자 계정 권한을 확인해주세요."),
    "UNGROUNDED_SCENARIO_REQUIREMENT": ("needs_information", "AI가 추출한 재현 조건을 제보 원문에서 확인하지 못했습니다.", "정확한 조작 순서와 완료 문구를 추가해주세요."),
    "INVALID_PLAN_OR_BASELINE": ("invalid_plan", "재현 계획 또는 비교 기준이 실행 전 검증을 통과하지 못했습니다.", "계획 검증 코드와 제보의 재현 조건을 확인해주세요."),
}


def build_diagnosis(report):
    run = report.get("run") or {}
    verdict = (run.get("verdict") or {}).get("value")
    evidence = run.get("evidence", [])
    codes = list(dict.fromkeys([code for code in [report.get("error_code"),
        *(e.get("blocker") for e in evidence)] if code]))
    questions = list(report.get("questions") or [])
    facts, seen, statuses = [], set(), set()
    for snapshot in report.get("observations", []):
        for request in snapshot.get("network", []):
            if request.get("status") is None and not request.get("blocker"):
                continue
            if (request.get("status") or 0) < 400 and not request.get("blocker"):
                continue
            key = (request.get("method"), request.get("url"), request.get("status"), request.get("blocker"))
            if key in seen or len(facts) >= 20:
                continue
            seen.add(key)
            statuses.add(request.get("status"))
            facts.append({"kind": "network_observation", "method": request.get("method"),
                "url": redact_text(request.get("url", "")), "status": request.get("status"),
                "blocker": request.get("blocker"), "observation_ids": [snapshot["id"]]})
    if verdict == "reproduced":
        category, summary = "reproduced", "고정된 조건의 독립 실행 두 번에서 신고된 증상이 관찰됐습니다."
    elif verdict == "not_reproduced":
        category, summary = "not_observed", "테스트한 조건의 독립 실행 두 번에서 정상 조건이 관찰됐고 신고된 증상은 관찰되지 않았습니다."
    elif report["status"] == "needs_info":
        category, summary = "needs_information", "재현에 필요한 정보가 부족해 실행 또는 판정을 보류했습니다."
    elif (run.get("verdict") or {}).get("reason_code") == "ATTEMPTS_DISAGREE":
        category, summary = "inconclusive", "두 독립 실행의 결과가 서로 달라 재현 여부를 확정하지 못했습니다."
        codes.append("ATTEMPTS_DISAGREE")
    else:
        category, summary, _ = _REASONS.get(codes[0] if codes else "", (
            "inconclusive", "실행을 완료하지 못해 재현 여부를 확정할 수 없습니다.", "작업의 오류 코드와 실행 증거를 확인해주세요."))
        if codes and codes[0].startswith("MODEL_") and codes[0] not in _REASONS:
            category, summary = "model_unavailable", "AI 처리 또는 작업의 모델 사용 예산 문제로 중단됐습니다."
    actions = []
    if verdict == "not_reproduced":
        actions.append({"code": "COMPARE_REPORTED_CONDITIONS", "owner": "reporter",
            "message": "제보 당시 계정 역할·세션·브라우저·앱 버전·입력 데이터를 현재 테스트 조건과 비교해주세요.",
            "required_fields": ["test_context_id", "browser_name", "app_version", "test_data"]})
    for code in codes:
        message = _REASONS.get(code, (None, None, "오류 코드와 실행 증거를 확인한 뒤 필요한 환경 또는 입력을 보완해주세요."))[2]
        actions.append({"code": code, "owner": "operator", "message": message, "required_fields": []})
        if code == "MODEL_RATE_LIMITED":
            delays = [call["retry_after_seconds"] for call in report.get("model_calls", [])
                      if call.get("status") == code and type(call.get("retry_after_seconds")) in (int, float)
                      and 0 <= call["retry_after_seconds"] <= 86400]
            if delays:
                actions[-1]["retry_after_seconds"] = max(delays)
    if verdict != "reproduced" and statuses & {401, 403}:
        actions.append({"code": "CHECK_TEST_ACCOUNT", "owner": "backend",
            "message": "401/403 응답이 관찰됐습니다. 제보한 역할의 테스트 계정과 접근 권한을 확인해주세요. 계정 문제로 원인이 확정된 것은 아닙니다.",
            "required_fields": ["test_context_id", "trusted_session_reference"]})
    if questions:
        actions.append({"code": "SUPPLY_MISSING_INFORMATION", "owner": "reporter",
            "message": "추가 확인 질문에 답한 뒤 새 작업을 요청해주세요.", "required_fields": ["report_details"]})
    if category == "inconclusive" and not actions:
        actions.append({"code": "REVIEW_INCONSISTENT_EVIDENCE", "owner": "developer",
            "message": "시도별 증거와 초기화 상태를 비교하고 간헐적인 동작인지 확인해주세요.", "required_fields": []})
    scenario = report.get("scenario") or {}
    attempts = []
    for item in evidence:
        completed = item["completed_steps"]
        steps = scenario.get("steps", [])
        attempts.append({"attempt": item["attempt"], "stage": item.get("stage", "unknown"),
            "completed_steps": completed, "total_steps": len(steps),
            "failed_step_index": completed if item.get("blocker") and completed < len(steps) else None,
            "next_step": steps[completed] if item.get("blocker") and completed < len(steps) else None,
            "blocker": item.get("blocker"), "ready": item["ready"],
            "expected_observed": item.get("expected_observed"), "symptom_observed": item["symptom_observed"],
            "elapsed_seconds": item.get("elapsed_seconds"), "evidence_ids": [item["id"]],
            "observation_ids": list(item.get("observation_ids", []))})
    return {"category": category, "summary": summary, "reason_codes": codes,
            "root_cause_confirmed": False, "attempts": attempts, "observed_facts": facts,
            "missing_information": questions, "next_actions": actions,
            "hypotheses": (report.get("analysis") or {}).get("hypotheses", []),
            "interpretation": "재현 안 됨은 테스트한 조건에서 증상을 확인하지 못했다는 뜻이며, 버그가 없거나 수정됐다는 뜻은 아닙니다."}
