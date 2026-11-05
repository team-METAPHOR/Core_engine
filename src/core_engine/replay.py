"""Deterministic replay of SDK-recorded reporter activity; the model only supplies the checks."""

import re
from urllib.parse import urlsplit

from .policy import PolicyError

# Line format rendered by the backend: "<n>ms ago <kind> <path>[ selector=<css> label=<text>[ post-click feedback=...]]".
_EVENT = re.compile(r"^\d+ms ago (navigation|click) (/\S*)(?: selector=(.+?) label=.*)?$")
MAX_REPLAY_STEPS = 18  # leaves the host entry navigation and one spare step within the 20-step limit


def recorded_events(activity):
    """Parse activity into ordered events, or () when replay is unsafe.

    Truncated activity (truncated=true) only lost older history: the remaining events are still the
    reporter's latest sequence, so replay starts from the earliest recorded page (scope-checked later)."""
    lines = [line.strip() for line in (activity or "").splitlines() if line.strip()]
    events = []
    for line in lines:
        match = _EVENT.match(line)
        if match is None:
            continue
        kind, path, selector = match.groups()
        if kind == "click" and not selector:
            return ()
        events.append({"kind": kind, "path": path, "selector": selector or "", "quote": line})
    return tuple(events)


# The report says an action was repeated ("세 번", "여러 번", "반복", "3 times").
REPEATED = re.compile(r"(\d+|두|세|네|다섯|여러|몇)\s*(번|회)|반복|연속|계속\s*눌|\b(\d+|several|multiple) times\b|\brepeated", re.I)


def _collapse(events, repeated):
    """Consecutive identical recorded clicks run once (twice when the report states a repeat)."""
    kept, run = [], 0
    for event in events:
        same = kept and event["kind"] == "click" and kept[-1]["kind"] == "click" and (
            kept[-1]["path"], kept[-1]["selector"]) == (event["path"], event["selector"])
        run = run + 1 if same else 1
        if run <= (2 if repeated else 1):
            kept.append(event)
    return tuple(kept)


def replay_steps(activity, entry_url, policy, report_text=""):
    """Recorded order as plan steps after the entry navigation; () when there is nothing safe to replay."""
    events = _collapse(recorded_events(activity), bool(REPEATED.search(report_text or "")))
    origin = "{0.scheme}://{0.netloc}".format(urlsplit(entry_url))
    try:
        for event in events:
            policy.check_url(origin + event["path"])
    except (PolicyError, ValueError):
        return ()
    steps, current, previous = [], urlsplit(entry_url).path, None
    for event in events:
        caused = previous is not None and previous["kind"] == "click"
        if event["path"] != current and (not caused or not steps):
            # The first recorded page, or a route change no recorded click explains.
            steps.append({"action": "navigate", "target": origin + event["path"], "value": "", "quote": event["quote"]})
        current = event["path"]
        if event["kind"] == "click":
            steps.append({"action": "click", "target": event["selector"], "value": "", "quote": event["quote"]})
        previous = event
    if not any(step["action"] == "click" for step in steps):
        return ()
    if len(steps) > MAX_REPLAY_STEPS:
        return ()
    return tuple(steps)


def replay_contract(steps, contract):
    """The replayed recording is the reported sequence; the grounded completion signal still applies."""
    return {"required_actions": [{"action": step["action"], "source_quote": step["quote"],
                                  "value": urlsplit(step["target"]).path if step["action"] == "navigate" else ""}
                                 for step in steps],
            "reported_observations": list((contract or {}).get("reported_observations", [])),
            "reported_actions_complete": False, "actions_complete": False, "completeness_basis": "recorded_replay",
            "completion_signal": (contract or {}).get("completion_signal")}


def compose(generated, steps, entry_url):
    """Recorded steps plus the model's grounded fills (planning validates their values verbatim) and checks."""
    replayed = [{key: step[key] for key in ("action", "target", "value")} for step in steps]
    clicks = [index for index, step in enumerate(replayed) if step["action"] == "click"]
    proposed = generated["steps"]
    for position, step in enumerate(proposed):
        if step["action"] != "fill":
            continue
        following = next((later["target"] for later in proposed[position + 1:] if later["action"] == "click"), None)
        index = next((i for i in clicks if replayed[i]["target"] == following), clicks[-1])
        replayed.insert(index, {"action": "fill", "target": step["target"], "value": step["value"]})
        clicks = [i + 1 if i >= index else i for i in clicks]
    return {"steps": [{"action": "navigate", "target": entry_url, "value": ""}, *replayed],
            **{key: generated[key] for key in ("ready", "expected", "symptom")}}
