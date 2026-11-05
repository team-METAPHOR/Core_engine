"""Guarded browser reproduction, deterministic checks and optional screenshot evidence."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import threading
import time
from typing import Awaitable, Callable
from uuid import uuid4

from .budget import BudgetLimits, RequestBudget
from .checks import Check, is_relative, note_matches, observe_check, text_value, wait_for_check
from .observation import BrowserOptions, Recorder
from .plan import BrowserPlan, TextCheck
from .policy import PolicyError, RunPolicy
from .reset import validate_reset
from .session import GuardedPage, launch_browser, browser_driver
from .transport import GuardedTransport
from .verdict import AttemptResult, Verdict, judge_attempts


@dataclass(frozen=True)
class Evidence:
    id: str
    attempt: int
    completed_steps: int
    ready: bool
    symptom_observed: bool | None
    blocker: str | None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    expected_observed: bool | None = None
    observation_ids: tuple[str, ...] = ()
    stage: str = "environment"
    elapsed_seconds: float = 0.0
    timeline: tuple[dict, ...] = ()
    completion_evidence: dict = field(default_factory=dict)
    blocked_requests: tuple[dict, ...] = ()
    state_changing_requests: tuple[dict, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class BrowserRun:
    program_id: str
    scope_revision: int
    plan_sha256: str
    attempts: tuple[AttemptResult, AttemptResult]
    verdict: Verdict
    evidence: tuple[Evidence, ...]


async def _perform_once(session, step, timeout_ms):
    from playwright.async_api import expect

    if step.action == "navigate":
        await session.navigate(step.target)
    elif step.action in {"reload", "back"}:
        await session.history_action(step.action)
    else:
        locator = session.page.locator("css=" + step.target)
        if step.action == "click":
            await locator.click(timeout=timeout_ms)
        elif step.action == "fill":
            # Credentials must come from trusted session setup, never model text.
            if await locator.get_attribute("type", timeout=timeout_ms) in {"password", "hidden"}:
                raise PolicyError("SENSITIVE_INPUT_UNSUPPORTED")
            await locator.fill(step.value, timeout=timeout_ms)
        else:
            await expect(locator).to_have_text(step.value, timeout=timeout_ms, use_inner_text=True)
    session.check()
    session.transport.policy.check_url(session.page.url)


async def _perform(session, step, timeout_ms):
    """Run one step. Time a request spends queued for the per-minute window does not count against
    the step timeout (the overall runtime still bounds it), and a target that is still missing gets
    one more try after the page settles (network idle) before the step fails."""
    from playwright.async_api import TimeoutError as BrowserTimeout

    if step.action not in {"click", "fill", "assert_text"}:
        return await _perform_once(session, step, timeout_ms)
    budget = session.transport.budget
    waited, settled, current = budget.rate_waited_seconds(), False, timeout_ms
    while True:
        try:
            return await _perform_once(session, step, current)
        except (BrowserTimeout, AssertionError):
            session.check()
            extra = budget.rate_waited_seconds() - waited
            if extra > 0.01:
                waited += extra
                current = max(100, min(int(extra * 1000), int(budget.remaining_seconds() * 1000)))
                continue
            if settled:
                raise
            settled, current = True, timeout_ms
            try:
                await session.page.wait_for_load_state("networkidle", timeout=timeout_ms)
            except BrowserTimeout:
                pass
            session.check()


def plan_digest(plan):
    return hashlib.sha256(json.dumps(asdict(plan), sort_keys=True, ensure_ascii=True).encode()).hexdigest()


class BrowserRunner:
    def __init__(self, policy: RunPolicy, limits: BudgetLimits = BudgetLimits(), *,
                 options: BrowserOptions | None = None, recorder: Recorder | None = None,
                 expected_browser_version: str | None = None):
        self.policy, self.limits = policy, limits
        self.options, self.recorder = options or BrowserOptions(), recorder
        self.expected_browser_version = expected_browser_version
        self.options.validate_scope(self.policy)

    def _browser_metadata(self, browser):
        if self.expected_browser_version and browser.version != self.expected_browser_version:
            raise PolicyError("BASELINE_BROWSER_MISMATCH")
        if self.recorder:
            self.recorder.browser_version = browser.version

    def _transport(self, is_authorized, cancelled, budget):
        return GuardedTransport(self.policy, budget or RequestBudget(self.limits), is_authorized,
                                cancelled or threading.Event(), session_headers=self.options.session_headers,
                                login_url=(self.policy.base_url.rstrip('/') + (self.options.login.submit_path or self.options.login.login_path))
                                if self.options.login else None)

    async def inspect(self, url: str, *, reset, is_authorized, cancelled=None, budget=None, steps=(), probe=()) -> dict | None:
        """Fresh observation for planning; reset(0) is the discovery pass.

        With `steps` (an already validated, scope-checked replay), they run after the entry navigation
        and the page after the last one is returned; None when a step cannot be completed."""
        from playwright.async_api import Error, async_playwright

        if self.recorder is None:
            raise ValueError("inspect requires a recorder")
        self.policy.check_url(url)
        transport = self._transport(is_authorized, cancelled, budget)
        try:
            async with asyncio.timeout(self.limits.max_runtime_seconds):
                transport.check()
                validate_reset(self.policy, reset)
                self.recorder.progress(attempt=0, phase="attempt_started")
                await self._reset(reset, 0)
                transport.check()
                async with browser_driver() as pw:
                    browser = await launch_browser(pw, self.options)
                    try:
                        self._browser_metadata(browser)
                        async with GuardedPage(browser, transport, self.options, self.recorder,
                                               self.options.step_timeout_ms) as session:
                            self.recorder.progress(attempt=0, phase="discovery")
                            try:
                                await session.navigate(url)
                            except (PolicyError, Error):
                                try:
                                    await session.capture(0, 0, "blocked")
                                except (PolicyError, Error):
                                    pass
                                raise
                            # Selectors matched by an element on the entry page or after any replayed step:
                            # a check whose target (a dialog opened by one step and closed by the next, say)
                            # is gone on the final screen is still grounded by an earlier one.
                            matched, transient = set(), []

                            async def note(snapshot):
                                if snapshot is not None:
                                    matched.update(element["selector"] for element in snapshot["elements"])
                                for selector in dict.fromkeys(probe):
                                    try:
                                        if selector not in matched and await session.page.locator("css=" + selector).count() > 0:
                                            matched.add(selector)
                                    except Error:
                                        pass
                            if steps:
                                await note(None)
                            for index, step in enumerate(steps):
                                try:
                                    await _perform(session, step, self.options.step_timeout_ms)
                                except (PolicyError, Error, AssertionError):
                                    return None
                                if index < len(steps) - 1:
                                    snapshot = await session.capture(0, index + 1, "replay_step")
                                    await note(snapshot)
                                    if snapshot is not None:
                                        transient.append(snapshot["id"])
                            result = await session.capture(0, len(steps), "replayed" if steps else "discovery")
                            if result is not None and steps:
                                await note(result)
                                result = {**result, "matched_selectors": sorted(matched), "step_snapshot_ids": transient}
                            if result is not None and probe:
                                # Check selectors (often container divs not listed in elements) that
                                # resolve to exactly one visible element on this screen.
                                resolved = []
                                for selector in dict.fromkeys(probe):
                                    try:
                                        locator = session.page.locator("css=" + selector)
                                        if await locator.count() == 1 and await locator.is_visible():
                                            resolved.append(selector)
                                    except Error:
                                        pass
                                result = {**result, "resolved_selectors": resolved}
                            session.check()
                            return result
                    finally:
                        await browser.close()
        except TimeoutError:
            raise PolicyError("RUNTIME_LIMIT") from None
        except Error:
            raise PolicyError("DISCOVERY_FAILED") from None
        finally:
            transport.close()

    @staticmethod
    async def _reset(reset, ordinal):
        try:
            ok = await reset(ordinal)
        except Exception:
            raise PolicyError("RESET_FAILED") from None
        if ok is not True:
            raise PolicyError("RESET_FAILED")

    async def run(
        self, plan: BrowserPlan, *, reset: Callable[[int], Awaitable[bool]],
        is_authorized: Callable[[], bool], cancelled: threading.Event | None = None,
        budget: RequestBudget | None = None,
    ) -> BrowserRun:
        """Callbacks/options are trusted host code, never supplied by a plan or model."""
        from playwright.async_api import Error, async_playwright

        plan.validate_scope(self.policy)
        if self.recorder:
            self.recorder.plan_sha256 = plan_digest(plan)
        transport = self._transport(is_authorized, cancelled, budget)
        attempts, evidence = [], []
        try:
            transport.check()
            validate_reset(self.policy, reset)
            async with asyncio.timeout(self.limits.max_runtime_seconds):
                async with browser_driver() as pw:
                    browser = await launch_browser(pw, self.options)
                    try:
                        self._browser_metadata(browser)
                        for ordinal in (1, 2):
                            transport.check()
                            await self._reset(reset, ordinal)
                            transport.check()
                            result, record = await self._attempt(browser, plan, transport, ordinal)
                            attempts.append(result)
                            evidence.append(record)
                    finally:
                        transport.close()
                        await browser.close()
        except (PolicyError, TimeoutError, Error) as exc:
            code = exc.code if isinstance(exc, PolicyError) else "RUNTIME_LIMIT" if isinstance(exc, TimeoutError) else "BROWSER_EXECUTION_FAILED"
            while len(attempts) < 2:
                record = Evidence(uuid4().hex, len(attempts) + 1, 0, False, None, code)
                evidence.append(record)
                attempts.append(AttemptResult(False, None, (record.id,), code))
        finally:
            transport.close()
        pair = (attempts[0], attempts[1])
        return BrowserRun(self.policy.program_id, self.policy.scope_revision, plan_digest(plan),
                          pair, judge_attempts(pair), tuple(evidence))

    async def _attempt(self, browser, plan, transport, ordinal):
        from playwright.async_api import Error, expect

        completed, ready, observed, expected, blocker = 0, False, None, None, None
        snapshots, warnings = [], []
        completion_evidence, before_outcomes, completion_tracker = {}, None, None
        baseline, relative_decided = {}, any(is_relative(check) for check in (plan.expected, plan.symptom))
        compared = {check.selector for check in (plan.expected, plan.symptom)
                    if isinstance(check, Check) and check.kind == "text"}
        started, stage, timeline = time.monotonic(), "environment", []
        # Check selectors matched by an element so far in this attempt (absence counts only after a match).
        checks, seen = (plan.ready, plan.expected, plan.symptom), set()
        responses = [(check.other_selector, check.selector, int(check.value))
                     for check in (plan.expected, plan.symptom) if isinstance(check, Check)
                     and check.kind == "response_status"]
        session = GuardedPage(browser, transport, self.options, self.recorder, plan.step_timeout_ms,
                              observed_responses=responses, attempt=ordinal)

        def mark(name, step_index=None):
            nonlocal stage
            stage = name
            timeline.append({"stage": name, "step_index": step_index,
                             "elapsed_seconds": round(time.monotonic() - started, 3)})
            if self.recorder:
                self.recorder.progress(attempt=ordinal, phase=name, step_index=step_index,
                    action=plan.steps[step_index].action if step_index is not None else None,
                    total_steps=len(plan.steps))
        if self.recorder:
            self.recorder.progress(attempt=ordinal, phase="attempt_started", total_steps=len(plan.steps))
        try:
            if self.options.login:
                mark("login")
            async with session:
                try:
                    for index, step in enumerate(plan.steps):
                        mark("action", index)
                        session.check()
                        snapshot = await session.capture(ordinal, index, "before")
                        if snapshot:
                            snapshots.append(snapshot["id"])
                        await note_matches(session.page, checks, seen)
                        if index == len(plan.steps) - 1 and step.action not in {"navigate", "reload", "back"}:
                            for selector in compared:
                                before_text = await text_value(session.page, selector)
                                if before_text is not None:
                                    baseline[selector] = before_text
                        if index == len(plan.steps) - 1 and step.action not in {"navigate", "reload", "back"} and getattr(plan.ready, "kind", "text") in {"text", "attribute"}:
                            try:
                                already_ready = await observe_check(session.page, plan.ready, session.network, seen=seen)
                            except PolicyError:
                                already_ready = False
                            completion_evidence = {"ready_before_final_action": already_ready,
                                                   "requests_before_final_action": len(session.network)}
                            if already_ready:
                                # A local handler can refresh the same completion text without
                                # making a request or changing a defective result. Observe the
                                # signal itself so that this still counts as fresh processing.
                                attribute = plan.ready.other_selector if isinstance(plan.ready, Check) and plan.ready.kind == "attribute" else None
                                completion_tracker = await session.page.locator("css=" + plan.ready.selector).evaluate_handle("""(node, attribute) => {
                                    const state = {mutations: 0};
                                    state.observer = new MutationObserver(records => state.mutations += records.length);
                                    state.observer.observe(node, attribute ? {attributes: true, attributeFilter: [attribute]}
                                        : {childList: true, characterData: true, subtree: true});
                                    return state;
                                }""", attribute)
                                try:
                                    before_outcomes = None if relative_decided else (
                                        await observe_check(session.page, plan.expected, session.network, seen=seen) if plan.expected else None,
                                        await observe_check(session.page, plan.symptom, session.network, seen=seen))
                                except PolicyError:
                                    before_outcomes = None
                        await _perform(session, step, plan.step_timeout_ms)
                        completed += 1
                        if self.recorder:
                            self.recorder.progress(attempt=ordinal, phase="action_completed", step_index=index,
                                                   action=step.action, total_steps=len(plan.steps))
                        snapshot = await session.capture(ordinal, index, "after")
                        if snapshot:
                            snapshots.append(snapshot["id"])
                        await note_matches(session.page, checks, seen)
                    mark("readiness")
                    try:
                        await wait_for_check(session.page, plan.ready, plan.step_timeout_ms, session.check, session.network,
                                             paused=transport.budget.rate_waited_seconds, seen=seen)
                        ready = True
                    except PolicyError as exc:
                        if exc.code != "STEP_OR_READINESS_FAILED" or plan.expected is None:
                            raise
                        # Every step completed: an unobserved completion signal alone does not block
                        # when the two outcome predicates are observable and exactly one holds.
                        warnings.append("READY_NOT_OBSERVED")
                    mark("outcome")
                    try:
                        if isinstance(plan.symptom, TextCheck):
                            await session.page.locator("css=" + plan.symptom.selector).wait_for(state="visible")
                        observed = await observe_check(session.page, plan.symptom, session.network, baseline, seen)
                        if plan.expected is not None:
                            expected = await observe_check(session.page, plan.expected, session.network, baseline, seen)
                            if observed == expected:
                                changed_text = await _unchanged_fallback(session.page, plan, baseline) if not observed else None
                                if changed_text is None:
                                    raise PolicyError("ORACLE_AMBIGUOUS")
                                # Neither literal value matched (values copied from the reporter's own
                                # session differ after reset): the same element's text before and after
                                # the final action decides a "should change but stays the same" report.
                                observed, expected, relative_decided = not changed_text, changed_text, True
                                warnings.append("RELATIVE_CHANGE_FALLBACK")
                    except (PolicyError, Error) as exc:
                        # A check selector that never matched an element cannot decide the outcome.
                        if not ready and getattr(exc, "code", None) != "CHECK_TARGET_NEVER_MATCHED":
                            raise PolicyError("STEP_OR_READINESS_FAILED") from None
                        raise exc
                    session.check()
                    if completion_evidence:
                        new_requests = len(session.network) > completion_evidence["requests_before_final_action"]
                        changed = before_outcomes != (expected, observed) if before_outcomes is not None else None
                        refreshed = None
                        if completion_tracker is not None and not new_requests:
                            refreshed = await completion_tracker.evaluate("""state => {
                                state.mutations += state.observer.takeRecords().length;
                                state.observer.disconnect();
                                return state.mutations > 0;
                            }""")
                        completion_evidence.update(new_requests_after_final_action=new_requests,
                                                   outcomes_changed_after_final_action=changed,
                                                   completion_signal_refreshed=refreshed)
                        if completion_evidence["ready_before_final_action"] and not new_requests and not refreshed and changed is False and not relative_decided:
                            raise PolicyError("STALE_COMPLETION_SIGNAL")
                    mark("completed")
                except PolicyError as exc:
                    blocker = exc.code
                except (Error, AssertionError):
                    blocker = session.blocked[0] if session.blocked else "STEP_OR_READINESS_FAILED"
                try:
                    snapshot = await session.capture(ordinal, completed, "blocked" if blocker else "verdict")
                    if snapshot:
                        snapshots.append(snapshot["id"])
                except (PolicyError, Error):
                    blocker = blocker or "EVIDENCE_CAPTURE_FAILED"
                    if stage == "completed":
                        stage = "evidence"
        except PolicyError as exc:
            blocker = exc.code
        if session.blocked:
            blocker = session.blocked[0]
        try:
            transport.check()
        except PolicyError as exc:
            blocker = exc.code
        if blocker:
            observed = expected = None
        if self.recorder:
            self.recorder.progress(attempt=ordinal, phase="attempt_failed" if blocker else "attempt_finished",
                                   total_steps=len(plan.steps))
        record = Evidence(uuid4().hex, ordinal, completed, ready, observed, blocker,
                          expected_observed=expected, observation_ids=tuple(snapshots), stage=stage,
                          elapsed_seconds=round(time.monotonic() - started, 3), timeline=tuple(timeline),
                          completion_evidence=completion_evidence,
                          blocked_requests=tuple(session.blocked_requests),
                          state_changing_requests=tuple(session.state_changing_requests),
                          warnings=tuple(warnings))
        return AttemptResult(blocker is None, observed, (record.id,), blocker), record


async def _unchanged_fallback(page, plan, baseline):
    """True/False when the shared text target changed across the final action; None when not applicable."""
    expected, symptom = plan.expected, plan.symptom
    if not all(isinstance(check, Check) and check.kind == "text" for check in (expected, symptom)):
        return None
    if expected.selector != symptom.selector or is_relative(expected) or is_relative(symptom):
        return None
    before, after = baseline.get(symptom.selector), await text_value(page, symptom.selector)
    if before is None or after is None:
        return None
    return before != after
