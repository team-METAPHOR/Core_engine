"""Ground reported requirements before planning; revisions cannot silently replace them."""

import copy
from urllib.parse import urlsplit
from .policy import canonical_url


def ground_contract(intake, report):
    fields = tuple(report.values())
    actions = intake.get("required_actions", [])
    signal = intake.get("completion_signal")
    for item in [*actions, *([signal] if signal else [])]:
        quote = item["source_quote"]
        if not quote.strip() or not any(quote in text for text in fields):
            raise ValueError("scenario requirement lacks an exact report quote")
        value = item.get("value", "")
        if value and value not in quote:
            raise ValueError("scenario requirement value is not grounded in its quote")
        if item.get("action") == "click" and value:
            raise ValueError("click requirements do not carry input values")
        if item.get("action") == "navigate":
            target = "https://scope.invalid" + value if value.startswith("/") and not value.startswith("//") else value
            try:
                canonical_url(target)
                valid = True
            except ValueError:
                valid = False
            if not valid or not value:
                raise ValueError("navigation needs an exact quoted URL or path")
    return {"required_actions": actions, "actions_complete": intake.get("actions_complete", False),
            "completion_signal": signal}


def planning_schema(schema, contract):
    """Constrain provider output using quoted report facts, never evaluator answers."""
    if not contract or not (contract["completion_signal"] or contract["required_actions"] or contract["actions_complete"]):
        return schema
    result = copy.deepcopy(schema)
    fields = result["properties"]["plan"]["anyOf"][0]["properties"]
    if contract["completion_signal"]:
        branch = copy.deepcopy(fields["ready"]["anyOf"][0])
        branch["properties"]["value"] = {"type": "string", "enum": [contract["completion_signal"]["value"]]}
        fields["ready"] = branch
    actions = contract["required_actions"]
    kinds = {"click", "fill", "navigate"}
    if contract["actions_complete"]:
        kinds = {actions[-1]["action"]} if actions else {"navigate"}
    elif actions and not any(item["action"] == "navigate" for item in actions):
        kinds.discard("navigate")
    branches = []
    for original in fields["final_action"]["anyOf"]:
        branch = copy.deepcopy(original)
        allowed = set(branch["properties"]["action"]["enum"]) & kinds
        if allowed:
            branch["properties"]["action"]["enum"] = sorted(allowed)
            branches.append(branch)
    fields["final_action"] = {"anyOf": branches}
    return result


def contract_error(plan, contract):
    if not contract or not (contract["completion_signal"] or contract["required_actions"] or contract["actions_complete"]):
        return None
    signal = contract["completion_signal"]
    if signal and (plan.ready.kind != "text" or plan.ready.value != signal["value"]):
        return "PLAN_REPORTED_COMPLETION_CHANGED", "ready"
    actual = [step for step in plan.steps[1:] if step.action != "assert_text"]
    required = contract["required_actions"]
    if sum(step.action == "navigate" for step in actual) > sum(item["action"] == "navigate" for item in required):
        return "PLAN_UNSUPPORTED_NAVIGATION", "steps"
    cursor = 0
    for step in actual:
        if cursor < len(required):
            item = required[cursor]
            same_value = step.action != "fill" or step.value == item["value"]
            if step.action == "navigate" and item["action"] == "navigate":
                parts = urlsplit(step.target)
                expected_url = parts.scheme + "://" + parts.netloc + item["value"] if item["value"].startswith("/") else item["value"]
                same_value = canonical_url(step.target) == canonical_url(expected_url)
            if step.action == item["action"] and same_value:
                cursor += 1
    if cursor != len(required) or (contract["actions_complete"] and len(actual) != len(required)):
        return "PLAN_REPORTED_SEQUENCE_CHANGED", "steps"
    return None
