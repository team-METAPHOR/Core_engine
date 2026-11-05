"""Deterministic replay of SDK-recorded reporter activity; the model only supplies the checks."""

import re
from urllib.parse import urlsplit

from .policy import PolicyError

# Line format rendered by the backend: "<n>ms ago <kind> <path>[ selector=<css> label=<text>[ post-click feedback=...]]".
_EVENT = re.compile(r"^\d+ms ago (navigation|click) (/\S*)(?: selector=(.+?) label=.*)?$")
MAX_REPLAY_STEPS = 18  # leaves the host entry navigation and one spare step within the 20-step limit


def recorded_events(activity):
    """Parse complete (not truncated) activity into ordered events, or () when replay is unsafe."""
    lines = [line.strip() for line in (activity or "").splitlines() if line.strip()]
    if not lines or "truncated=false" not in lines[0] + " " + (lines[1] if len(lines) > 1 else ""):
        return ()
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


def replay_steps(activity, entry_url, policy):
    """Recorded order as plan steps after the entry navigation; () when there is nothing safe to replay."""
    events = recorded_events(activity)
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
