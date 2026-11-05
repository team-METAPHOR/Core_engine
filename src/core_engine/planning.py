"""Pre-execution plan checks. Feedback never contains evaluator labels or runtime verdicts."""

from .qa_schema import decode_plan


FEEDBACK = {
    "PLAN_INVALID_CONTRACT": "Use the declared action/check contract, including true/false boolean values and distinct outcomes.",
    "PLAN_FINAL_ASSERTION": "Remove the final assert_text step. Put the final outcome in expected/symptom and use independent readiness.",
    "PLAN_READINESS_IS_OUTCOME": "Readiness equals an outcome. Use a separate completion signal that can hold for both buggy and normal behavior.",
    "PLAN_UNSUPPORTED_INPUT": "A fill value is absent from the supplied report/test_data. Copy the supplied literal exactly or ask for missing input data.",
}


class PlanValidationError(ValueError):
    def __init__(self, code, field="plan"):
        self.code = code
        self.field = field
        self.feedback = FEEDBACK[code]
        super().__init__(code)


def validate_candidate(data, report, *, entry_url, timeout_ms):
    """Validate a proposal before any reproduction action; never rewrite its intended outcome."""
    if data["steps"][-1]["action"] == "assert_text":
        raise PlanValidationError("PLAN_FINAL_ASSERTION", f"steps.{len(data['steps']) - 1}.action")
    try:
        plan = decode_plan(data, entry_url=entry_url, timeout_ms=timeout_ms)
    except ValueError:
        raise PlanValidationError("PLAN_INVALID_CONTRACT") from None
    if plan.ready in (plan.expected, plan.symptom):
        raise PlanValidationError("PLAN_READINESS_IS_OUTCOME", "ready")
    supplied = tuple(report.values())
    for index, step in enumerate(plan.steps):
        if step.action == "fill" and step.value and not any(step.value in field for field in supplied):
            raise PlanValidationError("PLAN_UNSUPPORTED_INPUT", f"steps.{index}.value")
    return plan
