"""Explicit canned-response demo by default; --live makes one paid API request."""

import argparse
from dataclasses import asdict
import json

from core_engine import AnalysisService, BugReport, Generation, OpenAIModel


def make_report(security=False):
    if security:
        return BugReport(
            "demo-access", "local-demo", "다른 테스트 계정의 문서가 보인다는 제보",
            "승인된 테스트 환경에서 합성 계정 A로 로그인 후 합성 계정 B의 문서 화면을 열었다고 제보함.",
            "문서 소유자에게만 열람 허용", "다른 계정 문서의 본문이 보였다고 제보함. 아직 실행 검증 전.",
        )
    return BugReport(
        "demo-search", "local-demo", "빈 검색 결과에서 이전 목록이 남음",
        "cat 검색 후 missing으로 다시 검색한다.", "검색 결과가 없으면 빈 목록을 표시", "이전 cat 결과가 남음",
    )


class FixtureModel:
    """Contract demonstration only: fixed data, no reasoning or network calls."""

    def __init__(self, security=False):
        self.security = security

    def generate(self, *, instructions, context, schema):
        if self.security:
            classification, severity = "security_suspected", "high"
            summary = "제보상 타 계정 문서 노출 가능성이 있으나 실행 검증되지 않았습니다."
            hypothesis = "문서 조회 시 서버의 소유권 확인이 누락됐을 가능성이 있습니다."
            title = "서버에서 문서 접근 권한 검증"
            description = "세션 사용자와 문서 접근 권한을 서버에서 확인하고 허용되지 않은 요청을 거절하는 방안을 검토합니다."
            checks = ["승인된 스테이징의 합성 계정 A/B로 본인 문서 접근 성공과 타 계정 문서 접근 거절을 확인합니다."]
        else:
            classification, severity = "general_bug", "low"
            summary = "제보상 빈 검색 결과에서 이전 목록이 남습니다. 원인과 수정은 검토가 필요합니다."
            hypothesis = "빈 응답일 때 목록 상태를 초기화하지 않는 분기가 있을 가능성이 있습니다."
            title = "빈 검색 결과에서도 목록 상태 갱신"
            description = "검색 성공 시 결과가 비어 있어도 목록 상태를 새 결과로 교체하도록 검토합니다."
            checks = ["결과가 있는 검색 다음에 빈 결과 검색을 수행해 이전 항목이 사라지는지 확인합니다.",
                      "연속 검색에서 늦게 도착한 이전 응답이 최신 결과를 덮지 않는지 확인합니다."]
        return Generation(json.dumps({
            "classification": classification, "confidence": "medium",
            "classification_reason": "제보 내용을 기준으로 한 데모용 고정 분류이며 실제 AI 판단이 아닙니다.",
            "severity": severity, "severity_reason": "제보된 영향에 대한 임시 추정입니다.", "summary": summary,
            "hypotheses": [{"description": hypothesis, "source_refs": ["report"]}],
            "solutions": [{"title": title, "description": description, "source_refs": ["report"],
                           "verification_steps": checks, "tradeoffs": ["기존 정상 동작과의 회귀 검증이 필요합니다."]}],
            "missing_information": ["관련 소스 코드와 실제 영향 범위"],
        }, ensure_ascii=False), "offline-fixture")


def analyze_example(*, run=None, live=False, security=False):
    model = OpenAIModel.from_env() if live else FixtureModel(security)
    return AnalysisService(model).analyze(make_report(security), run)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="실제 OpenAI API 1회 호출: 요금 발생 가능")
    parser.add_argument("--security", action="store_true", help="보안 의심 제보 예시")
    args = parser.parse_args()
    try:
        result = analyze_example(live=args.live, security=args.security)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"analysis_mode": "openai" if args.live else "fixture (실제 AI 아님)",
                      "analysis": asdict(result)}, ensure_ascii=False, indent=2))
    if result.status != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
