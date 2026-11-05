"""Bounded independent plan review. A critic can request changes, never decide a verdict."""

from .analysis_schema import _array, _enum, _object, _text
from .qa_schema import DATA_RULES


_ISSUE = _object(
        code=_enum("MISSING_STEP", "EXTRA_ACTION", "WRONG_READINESS", "WRONG_OUTCOME",
                   "UNSUPPORTED_INPUT", "UNNECESSARY_QUESTION", "CONTRACT_ERROR"),
        field=_enum("steps", "ready", "expected", "symptom", "input_data", "questions"),
        reason=_text(600), source_refs=_array(_enum("report", "page", "proposal", "validation"), 4, 1))
# Branch-specific fields make contradictory decisions impossible at the schema boundary.
REVIEW_SCHEMA = _object(assessment={"anyOf": [
    _object(decision=_enum("accept"), summary=_text(600)),
    _object(decision=_enum("revise"), summary=_text(600), issues=_array(_ISSUE, 5, 1)),
    _object(decision=_enum("needs_info"), summary=_text(600), questions=_array(_text(400), 3, 1)),
]})

REVIEW_PROMPT = DATA_RULES + """
Independently review a proposed web QA test BEFORE execution. You are a skeptical but fair reviewer,
not a verdict judge. Accept a sound plan; do not invent objections or demand unrelated testing.
Read the ORIGINAL report before the proposal. Check every supplied action, order, input literal,
completion signal and normal/defect predicate against the report and current initial DOM.
The proposal can be ready or needs_info. Request revision only for a concrete supported error:
- Missing setup actions or earlier positive/negative transitions from the reported sequence.
- Extra actions that change the state under test (e.g. clicking a control when its enabled state,
  after the reported trigger, is all that should be observed). Keep clicks explicitly required by
  the report; a control can legitimately be the trigger as well as the subject of a test.
- Readiness copied from the initial page instead of the reported final completion signal.
- A final assert_text step, or waiting for one of the outcomes as readiness: this prevents observing
  both normal and defective behavior. Move final completion to ready, outcome to expected/symptom.
- Inverted or inappropriate predicates. text reads innerText, not visible cropping; use clipped,
  overlap, enabled, visible or count for those properties. true/false mean the property itself.
- Invented/changed input values, or unnecessary questions when explicit requirements already exist.
Current corrected-looking DOM is not grounds to stop: the reported scenario should still be tested.
The page is the INITIAL state before actions. Do not reject a reported POST-action completion string
merely because the initial DOM shows a different string. If the report names the completion signal,
use it; replacing it with the page's initial status is wrong.
Do not infer missing requirements, future server responses, or bugs from a blank area alone.
ready must be independent of expected/symptom. Intermediate assert_text can wait between actions.
Execution is already provided by the host: it performs steps, waits for ready, then automatically
measures BOTH expected and symptom, twice in fresh sessions. These predicates ARE the final assertions.
All Check.value fields are STRINGS by contract: "true"/"false" for booleans, integer strings for count,
exact strings for text. Do not flag the use of string booleans. ready is a separate host check after
steps, not a missing step; requesting a ready step is an error. Click/navigate value must be "".
Never request an extra assert_text/wait_for_text/click to verify the final result. wait_for_text is not
an allowed action. A last click or fill followed by ready/expected/symptom is a complete test.
For a static issue, navigate alone is valid; readiness can be visibility of the affected content.
Use needs_info only for essential facts missing from report AND page, with concrete questions.
Check the WHOLE proposal even when validation already reports one error. Fixing readiness does not
justify leaving an extra click on the observed disabled control. Never append a click merely to move
an assert_text away from the last position; remove the redundant assertion and use the ready check.
Output assessment with one branch: accept has decision+summary only; revise adds 1..5 issues;
needs_info adds 1..3 questions. No issues/questions in the accept branch.
Each issue must cite supplied source_refs and name the specific mismatch, briefly in Korean.
You receive sanitized text/DOM only, not screenshots; never claim to have inspected image pixels.
Never output a replacement plan, code, browser actions, bug verdict or advice to bypass host checks.
"""


def review_context(report, snapshot, proposal, validation):
    # No image bytes, artifact paths, network logs, hidden answers or source code go to the critic.
    return {"report": report, "page": {"url": snapshot["url"], "elements": snapshot["elements"]},
            "proposal": {key: proposal[key] for key in ("status", "questions", "plan")}, "validation": validation}


def normalize_review(response):
    """Only call after REVIEW_SCHEMA validation; preserve a stable report representation."""
    item = response["assessment"]
    return {"decision": item["decision"], "summary": item["summary"],
            "issues": item.get("issues", []), "questions": item.get("questions", [])}
