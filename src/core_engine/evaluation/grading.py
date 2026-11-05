"""Independent scoring and explicit denominators, including failed started trials."""

import statistics


def verdict(result):
    return ((result.get("report") or {}).get("run") or {}).get("verdict", {}).get("value")


def grade(case, result, oracle, requests):
    observed = verdict(result)
    if case.category in {"bug", "normal"}:
        wanted = "reproduced" if case.category == "bug" else "not_reproduced"
        if result.get("status") != "completed" or observed != wanted:
            return {"passed": False, "reason": "VERDICT_MISMATCH"}
        checks = oracle.get("attempts", [])
        condition = "symptom" if case.category == "bug" else "normal"
        valid = (oracle.get("replay_completed") is True and len(checks) == 2
                 and all(item.get("exercised") is True and item.get(condition) is True for item in checks))
        return {"passed": valid, "reason": "VERIFIED" if valid else "INDEPENDENT_ORACLE_FAILED"}
    if case.category == "needs_info":
        passed = (result.get("status") == "needs_info" and bool(result.get("questions")) and observed is None)
        return {"passed": passed, "reason": "QUESTION_ROUTING_ONLY" if passed else "MISSING_INFORMATION_NOT_HANDLED"}
    expected_http = 503 if case.variant == "unavailable" else 401
    passed = (result.get("status") in {"failed", "inconclusive"} and observed is None
              and result.get("error_code") in {"HTTP_ERROR", "NAVIGATION_FAILED"}
              and any(item["status"] == expected_http for item in requests))
    return {"passed": passed, "reason": "ENVIRONMENT_RECOGNIZED" if passed else "ENVIRONMENT_NOT_HANDLED"}


def rate(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None}


def summarize(rows, calls, *, mode, retest_fixed=False):
    initial = [row for row in rows if row["kind"] == "initial"]
    started = [row for row in initial if row["status"] != "not_run"]
    bug = [row for row in started if row["category"] == "bug"]
    normal = [row for row in started if row["category"] == "normal"]
    missing = [row for row in started if row["category"] == "needs_info"]
    environment = [row for row in started if row["category"] == "environment"]
    conclusive = bug + normal
    durations = [row["elapsed_seconds"] for row in started]
    used = [call for call in calls if call.get("usage") is not None]
    retests = [row for row in rows if row["kind"] == "retest"]
    complete = all(row["status"] not in {"not_run", "interrupted"} for row in initial) and (
        not retest_fixed or all(row["status"] not in {"not_run", "interrupted"} for row in retests))
    harness_errors = sum(row["status"] == "harness_error" for row in rows)
    return {
        "mode": mode, "is_ai_evaluation": mode == "live",
        "complete": complete, "harness_errors": harness_errors,
        "valid_for_comparison": complete and not harness_errors and mode == "live",
        "planned_trials": len(initial), "started_trials": len(started),
        "not_run": len(initial) - len(started),
        "passed": sum(row["passed"] for row in started),
        "metrics": {
            "reproduction_success": rate(sum(row["passed"] for row in bug), len(bug)),
            "normal_success": rate(sum(row["passed"] for row in normal), len(normal)),
            "false_positive": rate(sum(row["verdict"] == "reproduced" for row in normal), len(normal)),
            "conclusive_coverage": rate(sum(row["verdict"] in {"reproduced", "not_reproduced"} for row in conclusive), len(conclusive)),
            "question_routing": rate(sum(row["passed"] for row in missing), len(missing)),
            "environment_handling": rate(sum(row["passed"] for row in environment), len(environment)),
            "fixed_pair_success": rate(sum(row["passed"] for row in retests), len(retests)) if retest_fixed else None,
        },
        "median_qa_seconds": statistics.median(durations) if durations else None,
        "provider_calls": len(calls), "calls_without_usage": len(calls) - len(used),
        "reported_input_tokens": sum(call["usage"]["input_tokens"] for call in used),
        "reported_output_tokens": sum(call["usage"]["output_tokens"] for call in used),
        "models": sorted({call["model"] for call in calls if call.get("model")}),
    }


def render_summary(summary, rows):
    title = "실제 AI 소규모 QA 평가" if summary["is_ai_evaluation"] else "고정 모델 연결 검증 — AI 성능 점수 아님"
    lines = [f"# {title}", "", f"최초 작업: {summary['started_trials']}/{summary['planned_trials']}건 실행, {summary['passed']}건 통과.",
             f"전체 완료: {summary['complete']}. 미실행: {summary['not_run']}건. 평가기 오류: {summary['harness_errors']}건.",
             f"실제 AI 비교용으로 사용 가능: {summary['valid_for_comparison']} (이 합성 세트 내 비교에 한함).", "",
             "| 지표 | 성공 또는 해당 건수 / 분모 | 비율 |", "|---|---:|---:|"]
    labels = {"reproduction_success": "독립 검증 재현 성공", "normal_success": "독립 검증 정상 판정",
              "false_positive": "정상 대상 오탐 (낮을수록 좋음)", "conclusive_coverage": "판정 완료",
              "question_routing": "추가 질문 분기", "environment_handling": "환경 장애 구분", "fixed_pair_success": "동일 계획 수정 확인"}
    for key, metric in summary["metrics"].items():
        if metric is not None:
            value = f"{metric['rate']:.1%}" if metric["rate"] is not None else "측정 없음"
            lines.append(f"| {labels[key]} | {metric['numerator']}/{metric['denominator']} | {value} |")
    lines += ["", f"모델 호출 {summary['provider_calls']}회. 보고된 입력/출력 토큰: {summary['reported_input_tokens']}/{summary['reported_output_tokens']}.",
              f"사용량 미보고 호출: {summary['calls_without_usage']}회. 이 호출의 비용은 0으로 간주하지 않습니다.",
              f"QA 실행 시간 중앙값(초): {summary['median_qa_seconds']}. 이미지 준비·독립 재실행 시간은 별도입니다.", "",
              "## 사례별 결과", "", "| 사례 | 반복 | 종류 | 상태 | 판정 | 통과 | 채점 사유 | 근거 |",
              "|---|---:|---|---|---|---|---|---|"]
    for row in rows:
        report = f"[보고서]({row['report_path']})" if row.get("report_path") else "없음"
        lines.append(f"| {row['case_id']} | {row['repeat']} | {row['kind']} | {row['status']} | {row['verdict'] or '-'} | {row['passed']} | {row['reason']} | {report} |")
    lines += ["", "## 해석 범위", "",
        "- 공개된 합성 개발용 사례입니다. 실제 서비스의 성능 또는 미공개 평가 점수로 일반화할 수 없습니다.",
        "- 반복 3회는 서로 다른 사례 3개가 아닙니다. 사례별 결과와 반복 변동을 함께 확인하세요.",
        "- 시작한 작업의 API/엔진 실패와 보류는 성공에서 제외하고 분모에는 포함합니다. 미실행은 별도 표시하며 전체 미완료 결과를 완전한 평가로 발표하지 않습니다.",
        "- 판정 완료율과 오탐률을 함께 확인하세요. 모두 보류하는 모델도 오탐률만 낮을 수 있습니다.",
        "- 추가 질문은 분기와 질문 존재만 자동 채점합니다. 질문의 적절성과 해결 방안 품질은 사람이 별도로 평가해야 합니다.",
        "- 이 실행 중 프롬프트나 엔진을 자동으로 수정하지 않습니다. 개선 전후는 별도 실행으로 비교하세요.",
        "- 금액은 계산하지 않습니다. 토큰 기록에는 캐시·공급자 과금 세부 항목이 없어 실제 청구액을 대신하지 못합니다.", ""]
    return "\n".join(lines)
