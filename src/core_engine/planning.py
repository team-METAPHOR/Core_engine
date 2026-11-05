"""Pre-execution plan checks. Feedback never contains evaluator labels or runtime verdicts."""

import re
from dataclasses import replace
from urllib.parse import urlsplit

from .checks import Check
from .qa_schema import decode_plan
from .scenario_contract import contract_error

# A report that names another screen ("상품 페이지에서", "상세 화면에서", "on the cart page").
SCREEN_MENTION = re.compile(r"(페이지|화면|상세|탭)\s*(에서|으로|로|에)|시작\s*(화면|페이지)|상품\s*상세"
                            r"|\b(on|from|in) the [\w -]{1,40}(page|screen)\b|\bstart(ing)? (page|screen)\b", re.I)


FEEDBACK = {
    "PLAN_INVALID_CONTRACT": "Use the declared action/check contract, including true/false boolean values and distinct outcomes.",
    "PLAN_FINAL_ASSERTION": "Remove the final assert_text step. Put the final outcome in expected/symptom and use independent readiness.",
    "PLAN_READINESS_IS_OUTCOME": "Readiness equals an outcome. Use a separate completion signal that can hold for both buggy and normal behavior.",
    "PLAN_UNSUPPORTED_INPUT": "A fill value is absent from the supplied report/test_data. Copy the supplied literal exactly or ask for missing input data.",
    "PLAN_REPORTED_COMPLETION_CHANGED": "Preserve the exact grounded completion_signal from scenario_contract in the ready text predicate.",
    "PLAN_REPORTED_SEQUENCE_CHANGED": "Preserve scenario_contract.required_actions in order, including literal inputs and repeat counts. Do not add interactions when actions_complete is true.",
    "PLAN_UNSUPPORTED_NAVIGATION": "Remove navigation/reload/back after the initial entry unless it is explicitly grounded in the reported required_actions. It can reset the state under test.",
    "PLAN_OVERBROAD_TEXT_TARGET": "Exact text equality against body/html/* is not a targeted assertion. Select the observed status/price/row element without changing the expected text or meaning.",
    "PLAN_UNSUPPORTED_RESPONSE_CHECK": "A response_status path/status must be explicitly supplied in the report/test_data; do not invent an endpoint or HTTP result.",
    "PLAN_SCREEN_NOT_REACHED": "The report acts on another screen, but the first interaction target is not on the entry page. Before it, add ONE click on the visible link/card for that screen/item from page.elements, or navigate to an in-scope href listed in page.elements. When page.elements shows no way there, navigate to the site root \"/\" and click the link/card whose text matches the named item.",
    "PLAN_TEXT_TARGET": "A click/fill target or check selector is plain visible text, not a CSS selector. Use the exact selector of the matching element from page.elements (match its text or name), or a CSS selector built from attributes such as [data-testid=...], [aria-label=...] or #id.",
    "PLAN_PROSE_CHECK_VALUE": "A text check value is a descriptive sentence, not text the page shows. Use the short exact inner text (at most 40 characters) of ONE observed element, e.g. the quantity number or the price, never a summary such as \"수량이 증가하고 합계도 변경됨\".",
    "PLAN_UNGROUNDED_CHECK": "A check selector does not match any element observed on the page (a text check: the final page.elements; a visible/enabled/clipped/count/attribute/overlap check: the final page or any replay_pages snapshot). A selector that matches nothing is never evidence that an element is hidden or absent. Copy the exact selector of the observed element (for a dialog/popup, the element listed in the replay_pages snapshot taken after it opened; prefer [data-testid=...] or #id), do not guess names or positional div > div paths.",
    "PLAN_UNREPORTED_REPEAT": "Do not duplicate the final trigger in both setup_steps and final_action. Consecutive identical interactions require explicitly repeated required_actions from the report.",
}


_TAGS = frozenset("""a abbr article aside b body button canvas caption code dd details dialog div dl dt em fieldset
figure footer form h1 h2 h3 h4 h5 h6 header html i iframe img input label legend li main nav ol optgroup option output
p pre section select small span strong summary svg table tbody td textarea tfoot th thead tr u ul video""".split())
_CSS_SYNTAX = re.compile(r"[#.\[\]:>~+*=()\"'|^$\\]")
_CUSTOM_TAG = re.compile(r"^[a-z][a-z0-9]*-[a-z0-9-]*$")
# Generic role words a model appends to visible text ("수량 늘리기 버튼", "수량 표시").
_ROLE_WORDS = frozenset("버튼 링크 표시 텍스트 입력란 입력창 필드 영역 아이콘 탭 button link field text label icon".split())
_CHECK_KINDS = {"text", "visible", "enabled", "clipped", "overlap", "count", "attribute"}


# A clause connector or a descriptive ending ("증가하고 ", "변하지 않고", "...변경됨") marks a sentence, not UI text.
_PROSE = re.compile(r"(하고|되고|않고|지고|면서|지만|는데)\s|변하지\s*않|그대로\s*(유지|있)")
_PROSE_END = re.compile(r"(됨|함|않음|된다|한다|않는다|보임)[.!]?$")
_STRUCTURAL = re.compile(r"^[a-z0-9\s>+~():-]+$")
MAX_CHECK_TEXT = 40


def prose_value(value):
    """True for a summary sentence written as an exact-text check value; no page shows it verbatim."""
    text = " ".join((value or "").split())
    return len(text) > MAX_CHECK_TEXT or bool(_PROSE.search(text)) or (
        len(text.split()) >= 3 and bool(_PROSE_END.search(text)))


def _canon(selector):
    return re.sub(r"\s*=\s*", "=", (selector or "").replace("'", '"')).strip()


def check_error(plan, page, *, require_present):
    """Raise when a check cannot be observed: prose text value, or a selector that matches nothing.

    `require_present` applies to the page the checks were written for (the screen after replay):
    a text check must match an element on that final screen; any other selector check (visible,
    enabled, clipped, count, attribute, overlap) must match an element on it or after any earlier
    replayed step (`matched_selectors`), because "not visible" on a selector that never existed is
    not evidence. Otherwise (the entry page, before any interaction) only guessed positional text
    check selectors (tags/nth-of-type only) are rejected."""
    final = {_canon(element["selector"]) for element in (page or {}).get("elements", [])}
    final |= {_canon(selector) for selector in (page or {}).get("resolved_selectors", [])}
    replayed = final | {_canon(selector) for selector in (page or {}).get("matched_selectors", [])}
    for key in ("ready", "expected", "symptom"):
        check = getattr(plan, key)
        kind = getattr(check, "kind", "text")
        if check is None or kind not in _CHECK_KINDS:
            continue
        if kind == "text" and prose_value(check.value):
            raise PlanValidationError("PLAN_PROSE_CHECK_VALUE", f"{key}.value")
        if page is None:
            continue
        grounded = final if kind == "text" else replayed
        for field in ("selector", "other_selector"):
            selector = getattr(check, field, "")
            if selector and _canon(selector) not in grounded and (
                    require_present or (kind == "text" and _STRUCTURAL.match(selector.strip()))):
                raise PlanValidationError("PLAN_UNGROUNDED_CHECK", f"{key}.{field}")


def grounded_fallback(plan, final, entry, report):
    """Last revision: observe one short element text after the replay that the report itself states.

    Only elements new or changed since the entry page qualify; the model's own checks are kept when
    observable. Returns None when nothing on the final page is grounded in the report."""
    def usable(check):
        if check is None:
            return False
        try:
            check_error(replace(plan, ready=check, expected=None, symptom=check), final, require_present=True)
        except PlanValidationError:
            return False
        return True
    stated = _norm(" ".join(report.values()))
    before = {(element["selector"], _norm(element.get("text"))) for element in (entry or {}).get("elements", [])}
    candidates = [element for element in final.get("elements", [])
                  if element.get("tag") not in {"a", "button", "input", "textarea", "select", "label", "option"}
                  and 0 < len(_norm(element.get("text"))) <= MAX_CHECK_TEXT and _norm(element.get("text")) in stated
                  and (element["selector"], _norm(element.get("text"))) not in before]
    symptom = plan.symptom if usable(plan.symptom) else None
    if symptom is None:
        if not candidates:
            return None
        best = max(candidates, key=lambda e: (e["selector"].startswith(("[data-testid", "#")), len(_norm(e["text"]))))
        symptom = Check("text", best["selector"], " ".join(best["text"].split()))
    expected = plan.expected if usable(plan.expected) and plan.expected != symptom else None
    ready = plan.ready if usable(plan.ready) and plan.ready not in (symptom, expected) else None
    if ready is None:
        ready = Check("visible", symptom.selector, "true") if symptom.kind == "text" else plan.ready
    return replace(plan, ready=ready, expected=expected, symptom=symptom)


def plain_text_target(target):
    """True for visible text such as "수량 늘리기 버튼"; syntactically it parses as type selectors but matches nothing."""
    text = target.strip()
    return bool(text) and not _CSS_SYNTAX.search(text) and any(
        token.lower() not in _TAGS and not _CUSTOM_TAG.match(token) for token in text.split())


def _norm(text):
    return " ".join((text or "").split()).casefold()


def _text_match(text, elements, inputs_only=False):
    """Selector of the element whose visible text/aria-label best matches: exact, then containment."""
    wanted = _norm(text)
    tokens = wanted.split()
    while len(tokens) > 1 and tokens[-1] in _ROLE_WORDS:
        tokens.pop()
    core = " ".join(tokens)
    best, best_score = None, 0
    for element in elements:
        if inputs_only and element.get("tag") not in {"input", "textarea"}:
            continue
        for label in (_norm(element.get("name")), _norm(element.get("text"))):
            if not label:
                continue
            score = (4 if label in {wanted, core} else 3 if core in label else
                     2 if len(label) >= 2 and label in wanted else 0)
            if score > best_score:
                best, best_score = element["selector"], score
    return best


def _ground_text_targets(data, targets, allow_text_targets):
    """Replace plain-text targets with a snapshot selector; reject the proposal when none matches."""
    elements = (targets or {}).get("elements", [])
    def resolve(value, field, inputs_only=False):
        if not plain_text_target(value):
            return value
        selector = _text_match(value, elements, inputs_only)
        if selector:
            return selector
        if allow_text_targets:
            return value  # last proposal: a runtime selector repair can still recover it
        raise PlanValidationError("PLAN_TEXT_TARGET", field)
    steps = [{**step, "target": resolve(step["target"], f"steps.{index}.target", step["action"] == "fill")}
             if step["action"] in {"click", "fill", "assert_text"} else step
             for index, step in enumerate(data["steps"])]
    grounded = {**data, "steps": steps}
    for key in ("ready", "expected", "symptom"):
        item = data.get(key)
        if isinstance(item, dict) and item.get("kind") in _CHECK_KINDS:
            item = {**item, "selector": resolve(item["selector"], f"{key}.selector")}
            if item["kind"] == "overlap":
                item["other_selector"] = resolve(item["other_selector"], f"{key}.other_selector")
            grounded[key] = item
    return grounded


class PlanValidationError(ValueError):
    def __init__(self, code, field="plan"):
        self.code = code
        self.field = field
        self.feedback = FEEDBACK[code]
        super().__init__(code)


def _seen_link(step, entry_url, page):
    """A navigate to a same-origin link path observed on the entry page is an implied precondition."""
    if step.action != "navigate" or not page:
        return False
    target, entry = urlsplit(step.target), urlsplit(entry_url)
    return (target.scheme, target.netloc) == (entry.scheme, entry.netloc) and not target.query and any(
        element.get("href") and element["href"] == target.path for element in page["elements"])


def _root_hop(steps, entry_url):
    """Site root, then a click on the named item: the 2-hop way to a start screen the entry page cannot reach."""
    if len(steps) < 4 or steps[1].action != "navigate" or steps[2].action != "click":
        return False
    target, entry = urlsplit(steps[1].target), urlsplit(entry_url)
    return (target.scheme, target.netloc) == (entry.scheme, entry.netloc) and target.path in {"", "/"} and not target.query


def _reaches_screen(plan, report, entry_url, page):
    """Return the plan without its implied navigation precondition, or raise when the screen is never reached."""
    steps = list(plan.steps)
    mentioned = any(SCREEN_MENTION.search(text) for text in report.values())
    if mentioned and _root_hop(steps, entry_url):
        # Exempt only from the reported sequence/navigation counts; the root is still scope-checked by the policy.
        return replace(plan, steps=(steps[0], *steps[3:]))
    if not page:
        return plan
    selectors = {element["selector"] for element in page["elements"]}
    if len(steps) > 2 and (_seen_link(steps[1], entry_url, page) or (
            mentioned and steps[1].action == "click" and steps[1].target in selectors
            and steps[2].action != "navigate" and steps[2].target not in selectors)):
        # Exempt only from the reported sequence/navigation counts; scope is still checked by the policy.
        return replace(plan, steps=(steps[0], *steps[2:]))
    first = next((step for step in steps[1:] if step.action != "navigate"), None)
    reached = any(step.action == "navigate" for step in steps[1:steps.index(first)]) if first else True
    if not reached and mentioned and first.action in {"click", "fill", "assert_text"} and first.target not in selectors:
        raise PlanValidationError("PLAN_SCREEN_NOT_REACHED", f"steps.{steps.index(first)}.target")
    return plan


def validate_candidate(data, report, *, entry_url, timeout_ms, contract=None, page=None, targets=None,
                       allow_text_targets=False, checks=True):
    """Validate a proposal before any reproduction action; never rewrite its intended outcome.

    `page` is the discovery snapshot; with it, an action on another reported screen must first reach
    that screen through a visible entry-page link/card or a seen in-scope link path. `targets` is the
    snapshot used to turn plain-text targets/check selectors into element selectors."""
    if data["steps"][-1]["action"] == "assert_text":
        raise PlanValidationError("PLAN_FINAL_ASSERTION", f"steps.{len(data['steps']) - 1}.action")
    data = _ground_text_targets(data, targets, allow_text_targets)
    try:
        plan = decode_plan(data, entry_url=entry_url, timeout_ms=timeout_ms)
    except ValueError:
        raise PlanValidationError("PLAN_INVALID_CONTRACT") from None
    if plan.ready in (plan.expected, plan.symptom):
        raise PlanValidationError("PLAN_READINESS_IS_OUTCOME", "ready")
    violation = contract_error(_reaches_screen(plan, report, entry_url, page), contract, report)
    if violation:
        raise PlanValidationError(*violation)
    if checks and not allow_text_targets:
        # The last proposal runs as-is; readiness/outcome observation still decides.
        check_error(plan, targets, require_present=False)
    supplied = tuple(report.values())
    for key in ("ready", "expected", "symptom"):
        check = getattr(plan, key)
        if check.kind == "text" and check.selector.strip().lower() in {"body", "html", "*", ":root", "html > body"}:
            raise PlanValidationError("PLAN_OVERBROAD_TEXT_TARGET", key)
        if check.kind == "response_status" and not all(any(value in field for field in supplied) for value in (check.selector, check.value)):
            raise PlanValidationError("PLAN_UNSUPPORTED_RESPONSE_CHECK", key)
    for index, step in enumerate(plan.steps):
        if step.action == "assert_text" and step.target.strip().lower() in {"body", "html", "*", ":root", "html > body"}:
            raise PlanValidationError("PLAN_OVERBROAD_TEXT_TARGET", f"steps.{index}.target")
        if step.action == "fill" and step.value and not any(step.value in field for field in supplied):
            raise PlanValidationError("PLAN_UNSUPPORTED_INPUT", f"steps.{index}.value")
    return plan
