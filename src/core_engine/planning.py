"""Pre-execution plan checks. Feedback never contains evaluator labels or runtime verdicts."""

from .qa_schema import decode_plan
from .scenario_contract import contract_error


FEEDBACK = {
    "PLAN_INVALID_CONTRACT": "Use the declared action/check contract, including true/false boolean values and distinct outcomes.",
    "PLAN_FINAL_ASSERTION": "Remove the final assert_text step. Put the final outcome in expected/symptom and use independent readiness.",
    "PLAN_READINESS_IS_OUTCOME": "Readiness equals an outcome. Use a separate completion signal that can hold for both buggy and normal behavior.",
    "PLAN_UNSUPPORTED_INPUT": "A fill value is absent from the supplied report/test_data. Copy the supplied literal exactly or ask for missing input data.",
    "PLAN_REPORTED_COMPLETION_CHANGED": "Preserve the exact grounded completion_signal from scenario_contract in the ready text predicate.",
    "PLAN_REPORTED_SEQUENCE_CHANGED": "Preserve scenario_contract.required_actions in order, including literal inputs and repeat counts. Do not add interactions when actions_complete is true.",
    "PLAN_UNSUPPORTED_NAVIGATION": "Remove navigation/reload after the initial entry unless it is explicitly grounded in the reported required_actions. It can reset the state under test.",
    "PLAN_UNSUPPORTED_RESPONSE_CHECK": "A response_status path/status must be explicitly supplied in the report/test_data; do not invent an endpoint or HTTP result.",
    "PLAN_UNREPORTED_REPEAT": "Do not duplicate the final trigger in both setup_steps and final_action. Consecutive identical interactions require explicitly repeated required_actions from the report.",
}


class PlanValidationError(ValueError):
    def __init__(self, code, field="plan"):
        self.code = code
        self.field = field
        self.feedback = FEEDBACK[code]
        super().__init__(code)


def validate_candidate(data, report, *, entry_url, timeout_ms, contract=None):
    """Validate a proposal before any reproduction action; never rewrite its intended outcome."""
    if data["steps"][-1]["action"] == "assert_text":
        raise PlanValidationError("PLAN_FINAL_ASSERTION", f"steps.{len(data['steps']) - 1}.action")
    try:
        plan = decode_plan(data, entry_url=entry_url, timeout_ms=timeout_ms)
    except ValueError:
        raise PlanValidationError("PLAN_INVALID_CONTRACT") from None
    if plan.ready in (plan.expected, plan.symptom):
        raise PlanValidationError("PLAN_READINESS_IS_OUTCOME", "ready")
    violation = contract_error(plan, contract)
    if violation:
        raise PlanValidationError(*violation)
    supplied = tuple(report.values())
    for key in ("ready", "expected", "symptom"):
        check = getattr(plan, key)
        if check.kind == "response_status" and not all(any(value in field for field in supplied) for value in (check.selector, check.value)):
            raise PlanValidationError("PLAN_UNSUPPORTED_RESPONSE_CHECK", key)
    for index, step in enumerate(plan.steps):
        if step.action == "fill" and step.value and not any(step.value in field for field in supplied):
            raise PlanValidationError("PLAN_UNSUPPORTED_INPUT", f"steps.{index}.value")
    return plan
