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
        if item.get("action") in {"click", "observe", "reload", "back"} and value:
            raise ValueError("click/observation requirements do not carry input values")
        if item.get("action") == "navigate":
            target = "https://scope.invalid" + value if value.startswith("/") and not value.startswith("//") else value
            try:
                canonical_url(target)
                valid = True
            except ValueError:
                valid = False
            if not valid or not value:
                raise ValueError("navigation needs an exact quoted URL or path")
    return {"required_actions": [item for item in actions if item["action"] != "observe"],
            "reported_observations": [item["source_quote"] for item in actions if item["action"] == "observe"],
            "reported_actions_complete": intake.get("actions_complete", False),
            "actions_complete": False, "completeness_basis": "provisional_before_page_plan",
            "completion_signal": signal}


def confirm_contract(plan, contract):
    """An intake guess must not constrain the unseen UI's submission mechanics."""
    if contract.get("completeness_basis") != "provisional_before_page_plan":
        return contract
    complete = contract.get("reported_actions_complete", False) and contract_error(
        plan, {**contract, "actions_complete": True}) is None
    return {**contract, "actions_complete": complete,
            "completeness_basis": "intake_and_page_plan_agree" if complete else "partial_reported_sequence"}


def planner_contract(contract):
    # Raw intake labels (including mistaken observations/completeness) are audit data,
    # not a second, contradictory instruction to the page-based planner.
    return {key: copy.deepcopy(contract[key]) for key in
            ("required_actions", "actions_complete", "completion_signal")}


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
    kinds = {"click", "fill", "navigate"} | {item["action"] for item in actions if item["action"] in {"reload", "back"}}
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
    if not contract:
        return None
    actual = [step for step in plan.steps[1:] if step.action != "assert_text"]
    required = contract["required_actions"]
    for action in ("reload", "back"):
        if sum(step.action == action for step in actual) > sum(item["action"] == action for item in required):
            return "PLAN_UNSUPPORTED_NAVIGATION", "steps"
    repeat = 1
    for index, step in enumerate(actual):
        repeat = repeat + 1 if index and step == actual[index - 1] else 1
        if repeat <= 1:
            continue
        permitted, streak = 1, 0
        for item in required:
            streak = streak + 1 if item["action"] == step.action and (
                step.action != "fill" or item["value"] == step.value) else 0
            permitted = max(permitted, streak)
        if repeat > permitted:
            return "PLAN_UNREPORTED_REPEAT", "steps"
    if not (contract["completion_signal"] or required or contract["actions_complete"]):
        return None
    signal = contract["completion_signal"]
    if signal and (plan.ready.kind != "text" or plan.ready.value != signal["value"]):
        return "PLAN_REPORTED_COMPLETION_CHANGED", "ready"
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
