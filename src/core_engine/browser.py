"""Guarded browser reproduction, deterministic checks and optional screenshot evidence."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import threading
from typing import Awaitable, Callable
from uuid import uuid4

from .budget import BudgetLimits, RequestBudget
from .checks import observe_check, wait_for_check
from .observation import BrowserOptions, Recorder
from .plan import BrowserPlan, TextCheck
from .policy import PolicyError, RunPolicy
from .session import GuardedPage, launch_browser
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


@dataclass(frozen=True)
class BrowserRun:
    program_id: str
    scope_revision: int
    plan_sha256: str
    attempts: tuple[AttemptResult, AttemptResult]
    verdict: Verdict
    evidence: tuple[Evidence, ...]


def plan_digest(plan):
    return hashlib.sha256(json.dumps(asdict(plan), sort_keys=True, ensure_ascii=True).encode()).hexdigest()


class BrowserRunner:
    def __init__(self, policy: RunPolicy, limits: BudgetLimits = BudgetLimits(), *,
                 options: BrowserOptions | None = None, recorder: Recorder | None = None):
        self.policy, self.limits = policy, limits
        self.options, self.recorder = options or BrowserOptions(), recorder

    def _transport(self, is_authorized, cancelled, budget):
        return GuardedTransport(self.policy, budget or RequestBudget(self.limits), is_authorized,
                                cancelled or threading.Event(), session_headers=self.options.session_headers)

    async def inspect(self, url: str, *, reset, is_authorized, cancelled=None, budget=None) -> dict:
        """Fresh read-only observation for planning; reset(0) is the discovery pass."""
        from playwright.async_api import async_playwright

        if self.recorder is None:
            raise ValueError("inspect requires a recorder")
        self.policy.check_url(url)
        transport = self._transport(is_authorized, cancelled, budget)
        try:
            async with asyncio.timeout(self.limits.max_runtime_seconds):
                transport.check()
                await self._reset(reset, 0)
                transport.check()
                async with async_playwright() as pw:
                    browser = await launch_browser(pw)
                    try:
                        async with GuardedPage(browser, transport, self.options, self.recorder) as session:
                            await session.navigate(url)
                            result = await session.capture(0, 0, "discovery")
                            session.check()
                            return result
                    finally:
                        await browser.close()
        except TimeoutError:
            raise PolicyError("RUNTIME_LIMIT") from None
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
        from playwright.async_api import async_playwright

        plan.validate_scope(self.policy)
        if self.recorder:
            self.recorder.plan_sha256 = plan_digest(plan)
        transport = self._transport(is_authorized, cancelled, budget)
        attempts, evidence = [], []
        try:
            transport.check()
            async with asyncio.timeout(self.limits.max_runtime_seconds):
                async with async_playwright() as pw:
                    browser = await launch_browser(pw)
                    try:
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
        except (PolicyError, TimeoutError) as exc:
            code = exc.code if isinstance(exc, PolicyError) else "RUNTIME_LIMIT"
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
        snapshots = []
        session = GuardedPage(browser, transport, self.options, self.recorder, plan.step_timeout_ms)
        try:
            async with session:
                try:
                    for index, step in enumerate(plan.steps):
                        session.check()
                        snapshot = await session.capture(ordinal, index, "before")
                        if snapshot:
                            snapshots.append(snapshot["id"])
                        if step.action == "navigate":
                            await session.navigate(step.target)
                        else:
                            locator = session.page.locator("css=" + step.target)
                            if step.action == "click":
                                await locator.click()
                            elif step.action == "fill":
                                # Credentials must come from trusted session setup, never model text.
                                if await locator.get_attribute("type") in {"password", "hidden"}:
                                    raise PolicyError("SENSITIVE_INPUT_UNSUPPORTED")
                                await locator.fill(step.value)
                            else:
                                await expect(locator).to_have_text(step.value, timeout=plan.step_timeout_ms, use_inner_text=True)
                        session.check()
                        completed += 1
                        snapshot = await session.capture(ordinal, index, "after")
                        if snapshot:
                            snapshots.append(snapshot["id"])
                    await wait_for_check(session.page, plan.ready, plan.step_timeout_ms, session.check)
                    ready = True
                    if isinstance(plan.symptom, TextCheck):
                        await session.page.locator("css=" + plan.symptom.selector).wait_for(state="visible")
                    observed = await observe_check(session.page, plan.symptom)
                    if plan.expected is not None:
                        expected = await observe_check(session.page, plan.expected)
                        if observed == expected:
                            raise PolicyError("ORACLE_AMBIGUOUS")
                    session.check()
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
        record = Evidence(uuid4().hex, ordinal, completed, ready, observed, blocker,
                          expected_observed=expected, observation_ids=tuple(snapshots))
        return AttemptResult(blocker is None, observed, (record.id,), blocker), record
