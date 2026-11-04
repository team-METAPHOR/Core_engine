"""Guarded anonymous browser reproduction with two fresh contexts."""

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
from .plan import BrowserPlan
from .policy import PolicyError, RunPolicy
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


@dataclass(frozen=True)
class BrowserRun:
    program_id: str
    scope_revision: int
    plan_sha256: str
    attempts: tuple[AttemptResult, AttemptResult]
    verdict: Verdict
    evidence: tuple[Evidence, ...]


class BrowserRunner:
    def __init__(self, policy: RunPolicy, limits: BudgetLimits = BudgetLimits()):
        self.policy, self.limits = policy, limits

    async def run(
        self, plan: BrowserPlan, *,
        reset: Callable[[int], Awaitable[bool]],
        is_authorized: Callable[[], bool],
        cancelled: threading.Event | None = None,
    ) -> BrowserRun:
        """Callbacks are trusted host code. A plan can never supply either callback."""
        from playwright.async_api import async_playwright

        plan.validate_scope(self.policy)
        budget = RequestBudget(self.limits)
        transport = GuardedTransport(self.policy, budget, is_authorized, cancelled or threading.Event())
        attempts: list[AttemptResult] = []
        evidence: list[Evidence] = []
        try:
            transport.check()
            async with asyncio.timeout(self.limits.max_runtime_seconds):
                async with async_playwright() as pw:
                    browser = await pw.chromium.launch(
                        headless=True,
                        proxy={"server": "http://127.0.0.1:1"},
                        args=["--proxy-bypass-list=<-loopback>", "--disable-background-networking",
                              "--disable-dns-prefetch", "--disable-quic",
                              "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
                    )
                    try:
                        for ordinal in (1, 2):
                            transport.check()
                            try:
                                reset_ok = await reset(ordinal)
                            except Exception:
                                raise PolicyError("RESET_FAILED") from None
                            if reset_ok is not True:
                                raise PolicyError("RESET_FAILED")
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
        plan_hash = hashlib.sha256(json.dumps(asdict(plan), sort_keys=True, ensure_ascii=True).encode()).hexdigest()
        return BrowserRun(self.policy.program_id, self.policy.scope_revision, plan_hash, pair, judge_attempts(pair), tuple(evidence))

    async def _attempt(self, browser, plan, transport, ordinal):
        from playwright.async_api import Error, expect

        context = await browser.new_context(service_workers="block", accept_downloads=False)
        blocked: list[str] = []
        completed = 0
        ready = False
        observed = None
        blocker = None
        workers: set[asyncio.Task] = set()

        def block(code):
            if not blocked:
                blocked.append(code)

        def check():
            transport.check()
            if blocked:
                raise PolicyError(blocked[0])

        async def route_request(route):
            task = None
            try:
                check()
                # No browser-direct request. The transport sends to a checked IP.
                task = asyncio.create_task(asyncio.to_thread(transport.fetch, route.request.url, route.request.method))
                workers.add(task)
                response = await asyncio.shield(task)
                check()
                await route.fulfill(status=response.status, headers=response.headers, body=response.body)
                if response.status >= 400:
                    block("HTTP_ERROR")
            except PolicyError as exc:
                block(exc.code)
                await route.abort("blockedbyclient")
            except Error:
                block("BROWSER_REQUEST_FAILED")
            finally:
                if task is not None and task.done():
                    workers.discard(task)

        async def websocket(ws):
            block("WEBSOCKET_UNSUPPORTED")
            await ws.close()

        try:
            await context.route("**/*", route_request)
            await context.route_web_socket("**/*", websocket)
            page = await context.new_page()
            page.set_default_timeout(plan.step_timeout_ms)
            page.on("pageerror", lambda _: block("PAGE_SCRIPT_ERROR"))
            page.on("download", lambda _: block("DOWNLOAD_UNSUPPORTED"))
            page.on("popup", lambda _: block("POPUP_UNSUPPORTED"))
            for step in plan.steps:
                check()
                if step.action == "navigate":
                    response = await page.goto(step.target, wait_until="load", timeout=plan.step_timeout_ms)
                    if response is None or response.status >= 400:
                        raise PolicyError("NAVIGATION_FAILED")
                else:
                    locator = page.locator("css=" + step.target)
                    if step.action == "click":
                        await locator.click()
                    elif step.action == "fill":
                        await locator.fill(step.value)
                    else:
                        await expect(locator).to_have_text(step.value, timeout=plan.step_timeout_ms, use_inner_text=True)
                check()
                completed += 1
            await expect(page.locator("css=" + plan.ready.selector)).to_have_text(
                plan.ready.text, timeout=plan.step_timeout_ms, use_inner_text=True,
            )
            ready = True
            locator = page.locator("css=" + plan.symptom.selector)
            await locator.wait_for(state="visible")
            observed = (await locator.inner_text()).strip() == plan.symptom.text.strip()
            check()
        except PolicyError as exc:
            blocker = exc.code
        except (Error, AssertionError):
            blocker = blocked[0] if blocked else "STEP_OR_READINESS_FAILED"
        finally:
            await context.close()
            if workers:
                await asyncio.gather(*workers, return_exceptions=True)
        if blocked:
            blocker = blocked[0]
        try:
            transport.check()
        except PolicyError as exc:
            blocker = exc.code
        if blocker:
            observed = None
        record = Evidence(uuid4().hex, ordinal, completed, ready, observed, blocker)
        result = AttemptResult(blocker is None, observed, (record.id,), blocker)
        return result, record
