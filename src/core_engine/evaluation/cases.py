"""Evaluator-owned cases and reference capture plans, not model context."""

from dataclasses import dataclass

from core_engine import BrowserPlan, Check, Step


@dataclass(frozen=True)
class Case:
    id: str
    family: str
    category: str
    variant: str
    description: str
    expected_result: str
    test_data: str = ""

    def report_text(self):
        return {key: getattr(self, key) for key in ("description", "expected_result", "test_data")}


REPORTS = {
    "search": (
        "상품 검색에서 머그를 검색한 뒤 없는상품을 검색하면 이전 머그 결과가 남는다고 제보합니다. 검색 완료 표시는 완료입니다.",
        "없는상품 검색이 완료되면 결과 영역이 검색 결과 없음으로 바뀌어야 합니다.",
        "합성 데이터: 머그는 결과가 있고 없는상품은 결과가 없습니다.",
    ),
    "filter": (
        "목록에서 의류만 보기로 필터를 적용한 다음 필터 초기화를 누르면 의류 목록만 남는다고 제보합니다. 처리 완료 표시는 완료입니다.",
        "필터 초기화가 끝나면 결과 영역에 티셔츠, 노트가 모두 보여야 합니다.",
        "전체 상품은 티셔츠, 노트이며 의류 상품은 티셔츠 하나입니다.",
    ),
    "clipping": (
        "상품 안내 화면에서 제목의 오른쪽 글자가 잘려 보입니다.",
        "상품 제목은 생략이나 가로 스크롤 없이 전체 글자가 보여야 합니다.",
        "화면 너비 1280px에서 확인합니다.",
    ),
    "overlap": (
        "페이지 이동 화면에서 이전 페이지와 다음 페이지 버튼이 서로 겹쳐 보입니다.",
        "이전 페이지 버튼과 다음 페이지 버튼은 서로 겹치지 않아야 합니다.",
        "화면 너비 1280px에서 확인합니다.",
    ),
}


CASES = tuple(
    Case(f"{family}_{category}", family, category, variant, *REPORTS[family])
    for family in REPORTS
    for category, variant in (("bug", "issue"), ("normal", "control"))
) + (
    Case("missing_expectation", "profile", "needs_info", "control", "", ""),
    Case("missing_search_data", "search", "needs_info", "control",
         "검색 결과가 나오지 않는다고 들었습니다. 검색어와 당시 계정은 모르겠습니다.",
         "결과가 나와야 한다고 들었지만 어떤 상품인지는 모릅니다."),
    Case("environment_unavailable", "search", "environment", "unavailable", *REPORTS["search"]),
    Case("environment_unauthorized", "search", "environment", "unauthorized", *REPORTS["search"]),
)


def reference_plan(family, url):
    """Used ONLY for source-image capture, offline fixtures, and fixture tests."""
    steps = [Step("navigate", url)]
    if family == "search":
        steps += [Step("fill", "#query", "머그"), Step("click", "#search"),
                  Step("assert_text", "#results", "머그"), Step("fill", "#query", "없는상품"),
                  Step("click", "#search")]
        expected, symptom = Check("text", "#results", "검색 결과 없음"), Check("text", "#results", "머그")
    elif family == "filter":
        steps += [Step("click", "#category"), Step("assert_text", "#results", "티셔츠"), Step("click", "#clear")]
        expected, symptom = Check("text", "#results", "티셔츠, 노트"), Check("text", "#results", "티셔츠")
    elif family == "clipping":
        expected, symptom = Check("clipped", "#headline", "false"), Check("clipped", "#headline", "true")
    elif family == "overlap":
        expected = Check("overlap", "#previous", "false", "#next")
        symptom = Check("overlap", "#previous", "true", "#next")
    else:
        raise ValueError("unknown reference family")
    ready = Check("text", "#status", "완료" if family in {"search", "filter"} else "준비 완료")
    return BrowserPlan(tuple(steps), ready, symptom, expected=expected)
