# Core_engine

Authorized web bug validation engine for team METAPHOR. This repository is being built in small, reviewable steps.

## Implemented in this first change

- `RunPolicy`: exact HTTP(S) origin, segment-aware allowed/excluded paths, read-only methods, conservative URL parsing, redirect checks, and DNS/IP validation. `resolve_target()` returns a numeric IP that a future transport **must pin** for the actual connection.
- `RequestBudget`: per-run request rate, concurrency, count, runtime, and response/download byte limits.
- `judge_attempts()`: evidence-gated verdict from two independent attempts. A blocked or conflicting attempt is inconclusive.

The policy and budget decisions are adapted from [IWANTGOHOME](https://github.com/Kowntaewook/IWANTGOHOME/tree/27f6835d2aaae2e5515eb19c360e2242c3308fcd), especially `programs.py`, `web.py`, and `research_policy.py`. The verdict rules adapt the evidence gates in `full_hunt/scenario.py`. This implementation narrows scope to one exact origin per run; it does not copy the MCP, CTF, or host-terminal approval workflow.

## Run checks

Requires Python 3.11 or newer. No third-party runtime dependency is needed for this step.

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## Trust boundary and next steps

`RunPolicy` must be created from a customer-approved scope revision supplied by the trusted backend, never from a reporter's form or an AI plan. `check_url()` alone does not guard network traffic: every actual browser request and redirect must use `resolve_target()` and connect to its returned numeric IP. A transport that resolves the hostname again can bypass this protection. The browser worker will also need isolated execution and an egress restriction outside the browser process.

Next reviewable steps are: guarded Playwright execution with fresh contexts and test-environment reset; redacted evidence storage; AI security triage and constrained reproduction planning; and a developer handoff with evidence-linked solution suggestions for general bugs. None of these are implemented in this first change. Customer authentication, persistent job queues, and the web API belong to the backend service.
