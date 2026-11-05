"""Screenshot-to-QA orchestration. No source-code edits, PRs, or model-executed code."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass, field, replace
import json
import hashlib
import re
import threading
import time
from typing import Awaitable, Callable

from .analysis import _INSTRUCTIONS as ANALYSIS_PROMPT
from .analysis_schema import analysis_schema, validate_output
from .artifacts import JobStore
from .browser import BrowserRunner, plan_digest
from .budget import BudgetLimits, RequestBudget
from .critic import REVIEW_PROMPT, REVIEW_SCHEMA, normalize_review, review_context, supported_revision
from .images import Screenshot
from .model import ModelError, strict_json
from .observation import BrowserOptions, Recorder
from .plan import Step
from .planning import PlanValidationError, validate_candidate
from .policy import PolicyError, RunPolicy, canonical_url
from .qa_schema import (INTAKE_SCHEMA, PLAN_DATA_SCHEMA, PLAN_SCHEMA, INTAKE_PROMPT, PLAN_PROMPT,
                        PLAN_REVISION_PROMPT, REPAIR_PROMPT, decode_plan, draft_plan, encode_plan, flatten_draft, plain_questions, repair_schema)
from .redaction import redact_text
from .reporting import render_report
from .scenario_contract import ground_contract, planning_schema, confirm_contract, planner_contract
from .diagnosis import build_diagnosis
from .test_export import export_playwright_test, required_test_environment
from .live import LiveStream


def _words(text):
    return {word for word in re.findall(r"[^\W\d_]{2,}", text.lower())}


def _leads_toward(selector, snapshot, report):
    """A navigation click must share report words and be the best-matching link/control on the page."""
    reported = " ".join(report.values()).lower()

    def score(element):
        return sum(word in reported for word in _words(element["text"] + " " + element["name"]))
    chosen = next(element for element in snapshot["elements"] if element["selector"] == selector)
    rivals = [element for element in snapshot["elements"]
              if (element["tag"] in {"a", "button"} or element["role"] in {"link", "button"})
              and chosen["text"] not in element["text"] and element["text"] not in chosen["text"]]
    return score(chosen) > 0 and all(score(element) <= score(chosen) for element in rivals)


@dataclass(frozen=True)
class QARequest:
    report_id: str
    program_id: str
    entry_url: str
    screenshot: Screenshot | None = field(default=None, repr=False)
    description: str = ""
    expected_result: str = ""
    test_data: str = ""
    reporter_activity: str = ""

    def __post_init__(self):
        for value in (self.report_id, self.program_id):
            if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
                raise ValueError("invalid report/program identifier")
        canonical_url(self.entry_url)
        if len(self.entry_url) > 2048:
            raise ValueError("entry URL is too long")
        for key, maximum in (("description", 8000), ("expected_result", 2000), ("test_data", 2000),
                             ("reporter_activity", 8192)):
            value = getattr(self, key)
            if not isinstance(value, str) or len(value) > maximum:
                raise ValueError("invalid QA request text")
            value.encode("utf-8")
        if self.screenshot is not None and not isinstance(self.screenshot, Screenshot):
            raise ValueError("screenshot must be validated")
        if self.screenshot is None and not self.description.strip():
            raise ValueError("a screenshot or description is required")

    def text(self):
        result = {key: redact_text(getattr(self, key)) for key in ("description", "expected_result", "test_data")}
        if self.reporter_activity:
            result["reporter_activity"] = redact_text(self.reporter_activity)
        return result


@dataclass(frozen=True)
class QALimits:
    max_model_calls: int = 6
    max_repairs: int = 2
    max_runtime_seconds: int = 180
    max_reported_tokens: int = 40_000
    max_plan_revisions: int = 2

    def __post_init__(self):
        for key, low, high in (("max_model_calls", 1, 10), ("max_repairs", 0, 2),
                               ("max_runtime_seconds", 1, 600), ("max_reported_tokens", 100, 100_000),
                               ("max_plan_revisions", 0, 2)):
            value = getattr(self, key)
            if type(value) is not int or not low <= value <= high:
                raise ValueError("invalid QA limit")


@dataclass(frozen=True)
class QAResult:
    job_id: str
    report_id: str
    program_id: str
    status: str
    report: dict
    questions: tuple[str, ...] = ()
    error_code: str | None = None


def clean_tree(value, redactor=redact_text):
    if isinstance(value, str):
        return redactor(value)
    if isinstance(value, (list, tuple)):
        return [clean_tree(item, redactor) for item in value]
    if isinstance(value, dict):
        return {key: clean_tree(item, redactor) for key, item in value.items()}
    return value


class _ModelCalls:
    def __init__(self, model, limits, guard, redactor=redact_text):
        self.model, self.limits, self.guard = model, limits, guard
        self.redactor = redactor
        self.records = []
        self.limit_events = []
        self.tokens = 0

    async def invoke(self, phase, instructions, context, schema, images=(), *, model=None):
        self.guard()
        provider = model if model is not None else self.model
        configured = getattr(provider, "model", None)
        configured = self.redactor(configured[:200]) if isinstance(configured, str) else None
        limit = "MODEL_CALL_LIMIT" if len(self.records) >= self.limits.max_model_calls else (
            "MODEL_TOKEN_LIMIT" if self.tokens >= self.limits.max_reported_tokens else None)
        if limit:
            self.limit_events.append({"phase": phase, "role": "critic" if phase == "plan_review" else "primary",
                "configured_model": configured, "error_code": limit, "call_started": False})
            raise PolicyError(limit)
        record = {"phase": phase, "role": "critic" if phase == "plan_review" else "primary",
                  "status": "started", "configured_model": configured, "model": None, "usage": None}
        self.records.append(record)
        call_started = time.monotonic()
        arguments = dict(instructions=instructions, context=clean_tree(context, self.redactor), schema=schema)
        if images:
            arguments["images"] = images
        task = asyncio.create_task(asyncio.to_thread(provider.generate, **arguments))
        try:
            while not task.done():
                self.guard()
                await asyncio.wait({task}, timeout=0.1)
            self.guard()
            generation = task.result()
            record.update(model=generation.model, usage=asdict(generation.usage) if generation.usage else None)
            if generation.usage:
                self.tokens += generation.usage.input_tokens + generation.usage.output_tokens
            if not isinstance(generation.text, str) or len(generation.text.encode()) > 64 * 1024:
                raise ValueError("invalid generated size")
            data = strict_json(generation.text)
            validate_output(data, schema)
            record["status"] = "completed"
            return clean_tree(data, self.redactor)
        except (ModelError, PolicyError) as exc:
            record["status"] = exc.code
            if isinstance(exc, ModelError) and exc.retry_after_seconds is not None:
                record["retry_after_seconds"] = exc.retry_after_seconds
            raise
        except asyncio.CancelledError:
            record["status"] = "interrupted"
            raise
        except (ValueError, TypeError, RecursionError):
            record["status"] = "MODEL_INVALID_OUTPUT"
            raise ModelError("MODEL_INVALID_OUTPUT") from None
        finally:
            record["elapsed_seconds"] = round(time.monotonic() - call_started, 3)
            if not task.done():
                # Cancels the waiter; an in-flight provider HTTP call may finish in its own timeout.
                # The thread never writes job state or artifacts.
                task.cancel()
            elif not task.cancelled():
                task.exception()  # Consume a completed provider failure even if authorization was revoked.


class QAService:
    def __init__(self, model, policy: RunPolicy, store: JobStore, *, options: BrowserOptions | None = None,
                 limits: QALimits = QALimits(), browser_limits: BudgetLimits | None = None, reviewer=None):
        self.model, self.policy, self.store = model, policy, store
        self.reviewer = reviewer
        self.options = options or BrowserOptions()
        self.options.validate_scope(self.policy)
        self.limits = limits
        self.browser_limits = browser_limits or BudgetLimits(max_runtime_seconds=120, max_requests_per_minute=60)

    async def run(self, request: QARequest, *, reset: Callable[[int], Awaitable[bool]],
                  is_authorized: Callable[[], bool], cancelled: threading.Event | None = None,
                  live: LiveStream | None = None,
                  _baseline=None, _parent=None, _job_created=None) -> QAResult:
        if live is not None and (not isinstance(live, LiveStream) or live.job_id is not None or live.closed):
            raise ValueError("supply a new LiveStream for each QA run")
        try:
            return await self._run(request, reset=reset, is_authorized=is_authorized, cancelled=cancelled,
                                   live=live, _baseline=_baseline, _parent=_parent, _job_created=_job_created)
        finally:
            if live:
                live._end("interrupted", error_code="LIVE_RUN_INTERRUPTED")

    async def _run(self, request, *, reset, is_authorized, cancelled, live, _baseline, _parent, _job_created):
        if not isinstance(request, QARequest) or request.program_id != self.policy.program_id:
            raise ValueError("request must belong to this program")
        self.policy.check_url(request.entry_url)
        cancelled = cancelled or threading.Event()
        started = time.monotonic()

        def guard():
            if cancelled.is_set():
                raise PolicyError("CANCELLED")
            if time.monotonic() - started >= self.limits.max_runtime_seconds:
                raise PolicyError("QA_RUNTIME_LIMIT")
            try:
                allowed = is_authorized()
            except Exception:
                raise PolicyError("AUTHORIZATION_UNAVAILABLE") from None
            if allowed is not True:
                raise PolicyError("AUTHORIZATION_REVOKED")

        # Authorization also covers external model processing, not just browser requests.
        guard()
        job_id = self.store.create()
        if live:
            live._bind(job_id, request.program_id, request.report_id)
        if _job_created:
            _job_created(job_id)
        recorder = Recorder(self.store, job_id, self.options, live=live)
        def event(kind, detail=None):
            self.store.event(job_id, kind, detail)
            if live:
                live._status(kind, detail)
        calls = _ModelCalls(self.model, self.limits, guard, self.options.redact)
        image_ref = self.store.image(job_id, request.screenshot) if request.screenshot else None
        self.store.write_json(job_id, "request.json", {"report_id": request.report_id,
            "program_id": request.program_id, "entry_url": request.entry_url,
            **clean_tree(request.text(), self.options.redact), "screenshot": image_ref})
        event("received", {"parent_job_id": _parent, "baseline_job_id": _baseline})
        intake, plan, run, analysis, repairs = None, None, None, None, []
        contract = None
        plan_validation = []
        plan_review = {"enabled": self.reviewer is not None, "status": "skipped_retest" if _baseline else
                       "not_reached" if self.reviewer is not None else "disabled",
                       "review": None, "reviewed_plan_sha256": None, "final_plan_sha256": None,
                       "error_code": None}
        baseline_report = None

        def finish(status, *, questions=(), error=None):
            questions = plain_questions(questions)
            plan_review["final_plan_sha256"] = plan_digest(plan) if plan else None
            classification = (analysis or intake or {}).get("classification", "needs_review")
            report = {
                "schema_version": "2", "job_id": job_id, "report_id": request.report_id,
                "program_id": request.program_id, "status": status, "title": (intake or {}).get("title", "QA 검증 결과"),
                "classification": classification, "human_review_required": True,
                "scope_revision": self.policy.scope_revision,
                "environment": {**self.options.public(), "browser_version": recorder.browser_version,
                                "state_changes": self.policy.state_changes.public() if self.policy.state_changes else None,
                                "login_profile_sha256": self.options.login.fingerprint() if self.options.login else None},
                "intake": intake, "scenario": clean_tree(encode_plan(plan)) if plan else None,
                "scenario_contract": contract,
                "plan_sha256": plan_digest(plan) if plan else None,
                "run": asdict(run) if run else None, "analysis": analysis,
                "questions": list(questions), "error_code": error,
                "observations": recorder.snapshots, "repairs": repairs, "model_calls": calls.records,
                "model_limit_events": calls.limit_events,
                "plan_validation": plan_validation,
                "plan_review": plan_review,
                "source_screenshot": image_ref, "baseline_job_id": _baseline, "parent_job_id": _parent,
                "regression": self._regression(baseline_report, run) if _baseline else None,
                "videos": recorder.videos, "video_enabled": self.options.record_video,
                "timings": {"total_seconds": round(time.monotonic() - started, 3),
                    "model_seconds": round(sum(c.get("elapsed_seconds", 0) for c in calls.records), 3)},
                "limitations": ["관찰된 증상과 실제 제품 요구사항의 일치는 사람이 검토해야 합니다.",
                    "재현 안 됨은 테스트한 조건에서 증상을 확인하지 못했다는 뜻입니다.",
                    "AI 원인·해결 방향은 가설이며 코드 수정이나 PR 생성을 수행하지 않습니다.",
                    "마스킹은 모든 개인정보를 탐지하지 않습니다. 승인된 합성 테스트 데이터를 사용하세요."],
            }
            report["diagnosis"] = build_diagnosis(report)
            report["test_export"] = {"status": "not_applicable", "artifact": None}
            if run and run.verdict.value == "reproduced" and plan:
                try:
                    source = export_playwright_test(plan, policy=self.policy, options=self.options, test_name=report["title"]).encode()
                    name = "reproduction.spec.ts"
                    self.store.write(job_id, name, source)
                    report["test_export"] = {"status": "ready", "plan_sha256": plan_digest(plan),
                        "artifact": {"name": name, "sha256": hashlib.sha256(source).hexdigest(),
                                     "bytes": len(source), "mime_type": "text/plain"},
                        "required_environment": required_test_environment(plan, self.options, policy=self.policy)}
                except (ValueError, OSError):
                    report["test_export"] = {"status": "failed", "artifact": None, "error_code": "TEST_EXPORT_FAILED"}
            report = clean_tree(report, self.options.redact)
            result = QAResult(job_id, request.report_id, request.program_id, status, report,
                              tuple(self.options.redact(q) for q in questions), error)
            self.store.write_json(job_id, "result.json", asdict(result))
            self.store.write(job_id, "report.md", render_report(report).encode("utf-8"))
            event(status, {"error_code": error})
            if live:
                live._end(status, error_code=error)
            return result

        try:
            async with asyncio.timeout(self.limits.max_runtime_seconds):
                if _baseline:
                    saved = self.store.read_json(_baseline, "scenario.json")
                    baseline_report = self.store.read_json(_baseline, "result.json")["report"]
                    if saved["program_id"] != request.program_id or saved["report_id"] != request.report_id:
                        raise ValueError("baseline identity mismatch")
                    if any(baseline_report["environment"].get(key) != self.options.public()[key]
                           for key in ("width", "height", "test_context_id", "session_mode")):
                        raise PolicyError("BASELINE_ENVIRONMENT_MISMATCH")
                    if baseline_report["environment"].get("login_profile_sha256") != (
                            self.options.login.fingerprint() if self.options.login else None):
                        raise PolicyError("BASELINE_ENVIRONMENT_MISMATCH")
                    validate_output(saved["plan"], PLAN_DATA_SCHEMA)
                    plan = decode_plan(saved["plan"], timeout_ms=saved["step_timeout_ms"])
                    if saved["plan_sha256"] != plan_digest(plan):
                        raise ValueError("baseline plan digest mismatch")
                    plan.validate_scope(self.policy)
                    intake = saved["intake"]
                    contract = baseline_report.get("scenario_contract")
                    if baseline_report["environment"].get("browser_name", "chromium") != self.options.browser_name:
                        raise PolicyError("BASELINE_ENVIRONMENT_MISMATCH")
                    if baseline_report["environment"].get("response_delay_ms", 0) != self.options.response_delay_ms:
                        raise PolicyError("BASELINE_ENVIRONMENT_MISMATCH")
                    if baseline_report["environment"].get("state_changes") != (
                            self.policy.state_changes.public() if self.policy.state_changes else None):
                        raise PolicyError("BASELINE_ENVIRONMENT_MISMATCH")
                    event("retesting", {"baseline_job_id": _baseline})
                else:
                    event("interpreting")
                    intake = await calls.invoke("intake", INTAKE_PROMPT,
                        {"report": request.text(), "has_screenshot": request.screenshot is not None,
                         "environment": self.options.public()},
                        INTAKE_SCHEMA, (request.screenshot,) if request.screenshot else ())
                    if intake["status"] == "needs_info" or not intake["expected_behavior"].strip() or intake["expectation_source"] == "unknown":
                        return finish("needs_info", questions=intake["questions"] or ["이 화면에서 기대하는 정상 동작을 알려주세요."])
                    if intake["questions"]:
                        return finish("needs_info", questions=intake["questions"])
                    try:
                        contract = ground_contract(intake, request.text())
                    except ValueError:
                        return finish("needs_info", questions=["조작 순서와 처리 완료를 알리는 문구를 원문 그대로 확인해주세요."],
                                      error="UNGROUNDED_SCENARIO_REQUIREMENT")
                budget = RequestBudget(self.browser_limits)
                runner = BrowserRunner(self.policy, self.browser_limits, options=self.options, recorder=recorder,
                    expected_browser_version=baseline_report["environment"].get("browser_version") if baseline_report else None)
                if plan is None:
                    event("discovering")
                    snapshot = await runner.inspect(request.entry_url, reset=reset, is_authorized=is_authorized,
                                                    cancelled=cancelled, budget=budget)
                    event("planning")
                    images = (recorder.image(snapshot),)
                    image_order = ["current_page"]
                    if request.screenshot is not None:
                        images += (request.screenshot,)
                        image_order.append("reporter_screenshot")
                    context = {"entry_url": redact_text(request.entry_url),
                               "intake": {k: v for k, v in intake.items() if k not in {"required_actions", "actions_complete", "completion_signal"}},
                               "report": request.text(),
                               "page": snapshot, "image_order": image_order, "scenario_contract": planner_contract(contract),
                               "environment": {**self.options.public(), "browser_version": recorder.browser_version,
                                   "state_changes": self.policy.state_changes.public() if self.policy.state_changes else None}}
                    proposal_schema = planning_schema(PLAN_SCHEMA, contract)
                    for proposal in range(self.limits.max_plan_revisions + 1):
                        generated = await calls.invoke("plan" if proposal == 0 else "plan_revision",
                                                       PLAN_PROMPT if proposal == 0 else PLAN_REVISION_PROMPT,
                                                       context, proposal_schema, images)
                        if generated["plan"] is not None:
                            generated = {**generated, "plan": flatten_draft(generated["plan"], entry_url=redact_text(request.entry_url))}
                        needs_info = generated["status"] == "needs_info" or generated["plan"] is None or generated["questions"]
                        candidate, feedback = None, None
                        if not needs_info:
                            try:
                                candidate = validate_candidate(generated["plan"], request.text(),
                                    entry_url=request.entry_url, timeout_ms=self.options.step_timeout_ms, contract=contract,
                                    # The reached-screen heuristic only asks for a revision; the last
                                    # proposal runs as-is and a runtime navigation repair can still help.
                                    page=snapshot if proposal < self.limits.max_plan_revisions else None)
                                self._validate_plan(candidate)
                                contract = confirm_contract(candidate, contract)
                                context = {**context, "scenario_contract": planner_contract(contract)}
                                proposal_schema = planning_schema(PLAN_SCHEMA, contract)
                            except PlanValidationError as exc:
                                diagnostic = {"proposal": proposal, "code": exc.code, "field": exc.field}
                                plan_validation.append(diagnostic)
                                event("plan_rejected", diagnostic)
                                feedback = {**diagnostic, "message": exc.feedback}
                        revise = False
                        if proposal == 0 and self.reviewer is not None:
                            plan_review["status"] = "reviewing"
                            plan_review["reviewed_plan_sha256"] = plan_digest(candidate) if candidate else None
                            event("reviewing_plan")
                            self.store.write_json(job_id, "review_proposal.json", {
                                "proposal": generated, "validation_feedback": feedback})
                            try:
                                review = await calls.invoke("plan_review", REVIEW_PROMPT,
                                    review_context(request.text(), snapshot, generated, feedback, context["environment"]), REVIEW_SCHEMA,
                                    model=self.reviewer)
                                review = normalize_review(review)
                            except asyncio.CancelledError:
                                plan_review["status"] = "interrupted"
                                raise
                            except (ModelError, PolicyError) as exc:
                                plan_review.update(status="failed", error_code=exc.code)
                                if calls.records and calls.records[-1]["phase"] == "plan_review":
                                    calls.records[-1]["status"] = exc.code
                                raise
                            plan_review["review"] = review
                            effective_review, ignored = supported_revision(review)
                            plan_review["ignored_issues"] = ignored
                            plan_review["status"] = review["decision"]
                            event("plan_reviewed", {"decision": review["decision"]})
                            if review["decision"] == "needs_info":
                                return finish("needs_info", questions=review["questions"])
                            revise = effective_review["decision"] == "revise"
                            context = {**context, "review_feedback": effective_review}
                        if needs_info and not revise:
                            return finish("needs_info", questions=generated["questions"] or ["재현 순서와 완료 상태를 알려주세요."])
                        if feedback or revise:
                            if proposal == self.limits.max_plan_revisions:
                                if self.reviewer is not None:
                                    plan_review["status"] = "revision_exhausted"
                                return finish("failed", error="INVALID_PLAN_OR_BASELINE")
                            context = {**context, "validation_feedback": feedback,
                                       "rejected_plan": draft_plan(generated["plan"]) if generated["plan"] else None,
                                       "previous_questions": generated["questions"]}
                            continue
                        plan = candidate
                        if self.reviewer is not None:
                            plan_review["status"] = "accepted" if proposal == 0 else "revised_unreviewed"
                            if proposal == 0 and plan_review.get("ignored_issues"):
                                plan_review["status"] = "accepted_ignoring_unanchored_review"
                            plan_review["final_plan_sha256"] = plan_digest(plan)
                        break
                for revision in range(self.limits.max_repairs + 1):
                    guard()
                    recorder.revision = revision
                    self._validate_plan(plan)
                    self.store.write_json(job_id, "scenario.json", {"report_id": request.report_id,
                        "program_id": request.program_id, "scope_revision": self.policy.scope_revision,
                        "plan": encode_plan(plan), "step_timeout_ms": plan.step_timeout_ms,
                        "plan_sha256": plan_digest(plan), "intake": intake})
                    event("running", {"revision": revision, "plan_sha256": plan_digest(plan)})
                    run = await runner.run(plan, reset=reset, is_authorized=is_authorized, cancelled=cancelled, budget=budget)
                    self.store.write_json(job_id, f"run_{revision}.json", asdict(run))
                    guard()
                    if run.verdict.value != "inconclusive" or revision == self.limits.max_repairs or _baseline:
                        break
                    failure = next((ev for ev in run.evidence if ev.blocker == "STEP_OR_READINESS_FAILED"
                                    and ev.completed_steps < len(plan.steps)), None)
                    if failure is None:
                        break
                    index = failure.completed_steps
                    step = plan.steps[index]
                    if step.action not in {"click", "fill", "assert_text"}:
                        break
                    snapshot = next((s for s in reversed(recorder.snapshots) if s["id"] in failure.observation_ids
                                     and s["phase"] == "blocked"), None)
                    if snapshot is None:
                        break
                    candidates = [item["selector"] for item in snapshot["elements"]
                                  if step.action != "fill" or item["tag"] in {"input", "textarea"}]
                    if not candidates:
                        break
                    event("replanning", {"step": index, "revision": revision})
                    repaired = await calls.invoke("repair", REPAIR_PROMPT,
                        {"intake": intake, "frozen_plan": encode_plan(plan), "failed_step_index": index,
                         "page": snapshot, "candidates": sorted(set(candidates))},
                        repair_schema(), (recorder.image(snapshot),))
                    if repaired["status"] in {"repair", "navigate_first"} and repaired["target"] not in set(candidates):
                        raise ModelError("MODEL_INVALID_OUTPUT")
                    if repaired["status"] == "navigate_first":
                        if len(plan.steps) >= 20:
                            break
                        if (repaired["questions"] or step.action != "click" or repaired["target"] == step.target
                                or any(item.get("kind") == "navigation" for item in repairs)
                                or not _leads_toward(repaired["target"], snapshot, request.text())):
                            raise ModelError("MODEL_INVALID_OUTPUT")
                        old_digest = plan_digest(plan)
                        inserted = Step("click", repaired["target"])
                        plan = replace(plan, steps=(*plan.steps[:index], inserted, *plan.steps[index:]))
                        repairs.append({"kind": "navigation", "step": index, "inserted": {"action": "click", "target": inserted.target},
                                        "reason": repaired["reason"], "from_plan": old_digest,
                                        "to_plan": plan_digest(plan), "observation_id": snapshot["id"]})
                        continue
                    if repaired["status"] == "needs_info":
                        return finish("needs_info", questions=repaired["questions"] or ["해당 조작 대상과 재현 순서를 확인해주세요."])
                    if repaired["status"] != "repair" or repaired["questions"] or not repaired["target"] or repaired["target"] == step.target:
                        break
                    old_digest = plan_digest(plan)
                    steps = list(plan.steps)
                    steps[index] = replace(step, target=repaired["target"])
                    plan = replace(plan, steps=tuple(steps))
                    repairs.append({"step": index, "reason": repaired["reason"], "from_plan": old_digest,
                                    "to_plan": plan_digest(plan), "observation_id": snapshot["id"]})
                guard()
                event("reporting")
                refs = {"report"} | {"evidence:" + ev.id for ev in run.evidence}
                try:
                    analysis = await calls.invoke("report", ANALYSIS_PROMPT,
                        {"report": request.text(), "intake": intake, "execution": asdict(run),
                         "allowed_source_refs": sorted(refs)}, analysis_schema(refs))
                    if analysis["confidence"] == "low":
                        analysis["classification"] = "needs_review"
                    if run.verdict.value != "reproduced" and analysis["confidence"] == "high":
                        analysis["confidence"] = "medium"
                except (ModelError, PolicyError) as exc:
                    if isinstance(exc, PolicyError) and exc.code not in {"MODEL_CALL_LIMIT", "MODEL_TOKEN_LIMIT"}:
                        raise
                    # A report-writing failure must never erase an already-observed verdict.
                    return finish("inconclusive" if run.verdict.value == "inconclusive" else "completed", error=exc.code)
                return finish("inconclusive" if run.verdict.value == "inconclusive" else "completed")
        except asyncio.CancelledError:
            cancelled.set()
            finish("cancelled", error="CANCELLED")
            raise
        except TimeoutError:
            cancelled.set()
            return finish("inconclusive", error="QA_RUNTIME_LIMIT")
        except (ModelError, PolicyError) as exc:
            status = "cancelled" if exc.code == "CANCELLED" else "inconclusive" if run or isinstance(exc, PolicyError) else "failed"
            return finish(status, error=exc.code)
        except ValueError:
            return finish("failed", error="INVALID_PLAN_OR_BASELINE")
        except Exception:
            # Backend receives a stable code; no provider/page exception body is exposed.
            return finish("failed", error="QA_EXECUTION_FAILED")

    def _validate_plan(self, plan):
        plan.validate_scope(self.policy)
        for step in plan.steps:
            if step.value and redact_text(step.value) != step.value:
                raise ValueError("sensitive generated action value")

    async def retest(self, job_id: str, *, reset, is_authorized, cancelled=None, live=None):
        request = self._load_request(job_id)
        return await self.run(request, reset=reset, is_authorized=is_authorized, cancelled=cancelled, live=live, _baseline=job_id)

    async def resume(self, job_id: str, answers: str, *, reset, is_authorized, cancelled=None, live=None):
        previous = self.store.read_json(job_id, "result.json")
        if previous["status"] != "needs_info" or not isinstance(answers, str) or not answers.strip():
            raise ValueError("resume requires a needs_info job and answers")
        request = self._load_request(job_id)
        request = replace(request, description=request.description + "\n추가 답변:\n" + answers)
        return await self.run(request, reset=reset, is_authorized=is_authorized, cancelled=cancelled, live=live, _parent=job_id)

    def _load_request(self, job_id):
        data = self.store.read_json(job_id, "request.json")
        if data["program_id"] != self.policy.program_id:
            raise ValueError("job belongs to a different program")
        data["screenshot"] = self.store.read_image(job_id, data["screenshot"]) if data["screenshot"] else None
        return QARequest(**data)

    @staticmethod
    def _regression(previous, run):
        if not previous or not previous.get("run") or run is None:
            return "no_confirmed_baseline"
        before, after = previous["run"]["verdict"]["value"], run.verdict.value
        if after == "inconclusive":
            return "inconclusive"
        if before == "reproduced":
            return "resolved_in_test" if after == "not_reproduced" else "still_reproduced"
        if before == "not_reproduced":
            return "newly_reproduced" if after == "reproduced" else "unchanged"
        return "no_confirmed_baseline"
