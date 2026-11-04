"""Explicit canned-response demo by default; --live makes one paid API request."""

import argparse
from dataclasses import asdict
import json

from core_engine import AnalysisService, BugReport, Generation, OpenAIModel


def make_report(visual=False):
    if visual:
        return BugReport(
            "demo-access", "local-demo", "좁은 화면에서 버튼이 제목과 겹침",
            "모바일 너비에서 화면을 열면 제목과 확인 버튼이 겹친다고 제보함.",
            "제목과 버튼이 서로 겹치지 않음", "제목과 버튼이 겹친다고 제보함. 아직 실행 검증 전.",
        )
    return BugReport(
        "demo-search", "local-demo", "빈 검색 결과에서 이전 목록이 남음",
        "cat 검색 후 missing으로 다시 검색한다.", "검색 결과가 없으면 빈 목록을 표시", "이전 cat 결과가 남음",
    )


class FixtureModel:
    """Contract demonstration only: fixed data, no reasoning or network calls."""

    def __init__(self, visual=False):
        self.visual = visual

    def generate(self, *, instructions, context, schema):
        if self.visual:
            classification, severity = "visual", "high"
            summary = "제보상 좁은 화면에서 요소 겹침이 있으나 실행 검증되지 않았습니다."
            hypothesis = "고정 위치와 좁은 화면 너비가 충돌했을 가능성이 있습니다."
            title = "반응형 레이아웃 검토"
            description = "제목과 버튼 영역을 분리하고 좁은 화면에서 줄바꿈되도록 레이아웃을 검토합니다."
            checks = ["모바일과 데스크톱 너비에서 요소가 겹치지 않는지 확인합니다."]
        else:
            classification, severity = "functional", "low"
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


def analyze_example(*, run=None, live=False, visual=False):
    model = OpenAIModel.from_env() if live else FixtureModel(visual)
    return AnalysisService(model).analyze(make_report(visual), run)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="실제 OpenAI API 1회 호출: 요금 발생 가능")
    parser.add_argument("--visual", action="store_true", help="시각적 버그 제보 예시")
    args = parser.parse_args()
    try:
        result = analyze_example(live=args.live, visual=args.visual)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({"analysis_mode": "openai" if args.live else "fixture (실제 AI 아님)",
                      "analysis": asdict(result)}, ensure_ascii=False, indent=2))
    if result.status != "completed":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
