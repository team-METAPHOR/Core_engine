"""Bounded condition selection and isolated QA jobs over a trusted backend registry."""

import asyncio
from dataclasses import asdict, dataclass, field, replace
import re
import threading
import time

from .analysis_schema import _array, _enum, _object, _text
from .model import ModelError
from .observation import BrowserOptions
from .policy import PolicyError
from .reset import bind_condition_reset
from .qa import QAService, QALimits, _ModelCalls
from .qa_schema import DATA_RULES
from .redaction import redact_text
from .reporting import escape


@dataclass(frozen=True)
class TestCondition:
    id: str
    label: str
    options: BrowserOptions = field(default_factory=BrowserOptions)
    entry_url: str | None = None

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", self.id):
            raise ValueError("invalid condition ID")
        if not isinstance(self.label, str) or not 1 <= len(self.label) <= 200 or redact_text(self.label) != self.label:
            raise ValueError("condition label must be public, bounded text")
        if not isinstance(self.options, BrowserOptions):
            raise ValueError("condition needs trusted browser options")

    def public(self):
        return {"id": self.id, "label": self.label, **self.options.public()}


@dataclass(frozen=True)
class ConditionLimits:
    max_conditions: int = 4
    max_model_calls: int = 24
    max_reported_tokens: int = 120_000
    max_runtime_seconds: int = 600

    def __post_init__(self):
        for key, low, high in (("max_conditions", 1, 8), ("max_model_calls", 1, 64),
                               ("max_reported_tokens", 100, 400_000), ("max_runtime_seconds", 1, 1800)):
            if type(getattr(self, key)) is not int or not low <= getattr(self, key) <= high:
                raise ValueError("invalid condition limit")


@dataclass(frozen=True)
class ConditionsResult:
    job_id: str
    report_id: str
    program_id: str
    status: str
    report: dict


class _SharedBudget:
    def __init__(self, limits, started):
        self.limits, self.started = limits, started
        self.records, self.tokens = [], 0
        self.lock = threading.Lock()

    def check(self):
        if time.monotonic() - self.started >= self.limits.max_runtime_seconds:
            raise PolicyError("CONDITION_RUNTIME_LIMIT")
        if len(self.records) >= self.limits.max_model_calls:
            raise PolicyError("CONDITION_MODEL_CALL_LIMIT")
        if self.tokens >= self.limits.max_reported_tokens:
            raise PolicyError("CONDITION_MODEL_TOKEN_LIMIT")

    def wrap(self, provider, role):
        shared = self

        class CountedModel:
            def generate(self, **kwargs):
                with shared.lock:
                    shared.check()
                    record = {"role": role, "status": "started", "model": None, "usage": None}
                    shared.records.append(record)
                started = time.monotonic()
                try:
                    result = provider.generate(**kwargs)
                    with shared.lock:
                        record.update(status="completed", model=result.model,
                                      usage=asdict(result.usage) if result.usage else None)
                        if result.usage:
                            shared.tokens += result.usage.input_tokens + result.usage.output_tokens
                    return result
                except (ModelError, PolicyError) as exc:
                    with shared.lock:
                        record["status"] = exc.code
                    raise
                except Exception:
                    with shared.lock:
                        record["status"] = "MODEL_UNAVAILABLE"
                    raise ModelError("MODEL_UNAVAILABLE") from None
                finally:
                    with shared.lock:
                        record["elapsed_seconds"] = round(time.monotonic() - started, 3)

        return CountedModel()

    def snapshot(self):
        with self.lock:
            return [dict(record) for record in self.records]


_SELECT = DATA_RULES + """
Choose a small ordered set of diagnostic test conditions from the supplied TRUSTED registry.
Each ID represents a backend-configured account/session/browser/viewport/network-delay combination.
Return only supplied IDs; you cannot create credentials, environments, scopes or new browser settings.
Include useful contrasts for the report (for example reported role vs another registered role, or
the same role across browser engines). A condition's hypothesis is tentative, never a bug verdict.
Use all relevant supplied facts, preserve the stated role, and avoid arbitrary combinations.
If an essential account/environment is not registered, ask a concrete question and use needs_info.
Do not invent credentials or claim an unavailable browser/account was tested. At most the configured
number of conditions, without duplicate IDs. Every selected condition will be independently planned
and executed twice under its own trusted options. No evaluator answers or previous results are given.
"""


class ConditionsService:
    def __init__(self, model, policy, store, conditions, *, reviewer=None,
                 limits=ConditionLimits(), qa_limits=QALimits(), browser_limits=None):
        if not isinstance(conditions, tuple) or not 1 <= len(conditions) <= 16 or not all(isinstance(c, TestCondition) for c in conditions):
            raise ValueError("supply 1..16 registered conditions as a tuple")
        if len({c.id for c in conditions}) != len(conditions):
            raise ValueError("condition IDs must be unique")
        for condition in conditions:
            if condition.entry_url:
                policy.check_url(condition.entry_url)
        self.model, self.reviewer, self.policy, self.store = model, reviewer, policy, store
        self.conditions, self.limits, self.qa_limits, self.browser_limits = conditions, limits, qa_limits, browser_limits

    async def run(self, request, *, reset, is_authorized, cancelled=None, select=True, live_factory=None):
        if request.program_id != self.policy.program_id or type(select) is not bool:
            raise ValueError("invalid matrix request")
        self.policy.check_url(request.entry_url)
        cancelled = cancelled or threading.Event()
        started = time.monotonic()
        budget = _SharedBudget(self.limits, started)

        def guard():
            if cancelled.is_set():
                raise PolicyError("CANCELLED")
            if time.monotonic() - started >= self.limits.max_runtime_seconds:
                raise PolicyError("CONDITION_RUNTIME_LIMIT")
            try:
                allowed = is_authorized()
            except Exception:
                raise PolicyError("AUTHORIZATION_UNAVAILABLE") from None
            if allowed is not True:
                raise PolicyError("AUTHORIZATION_REVOKED")

        guard()
        job_id = self.store.create()
        self.store.write_json(job_id, "request.json", {"report_id": request.report_id, "program_id": request.program_id,
            "entry_url": redact_text(request.entry_url), **request.text(), "conditions": [c.public() for c in self.conditions]})
        selected, rows, selection, error = [], [], None, None
        registry = {c.id: c for c in self.conditions}

        def update_row(row, result):
            rep = result["report"]
            row.update(status=result["status"], verdict=(rep.get("run") or {}).get("verdict", {}).get("value"),
                error_code=result.get("error_code"), diagnosis=rep["diagnosis"], environment=rep["environment"],
                plan_sha256=rep["plan_sha256"], videos=rep["videos"], test_export=rep["test_export"],
                report_path="../" + result["job_id"] + "/report.md")

        def finish(status):
            for row in rows:
                if row["status"] == "running":
                    try:
                        update_row(row, self.store.read_json(row["job_id"], "result.json"))
                    except (FileNotFoundError, KeyError):
                        row.update(status="inconclusive", error_code=error or "CONDITION_INTERRUPTED")
            completed_ids = {row["condition_id"] for row in rows}
            for candidate in selected:
                if candidate["condition_id"] not in completed_ids:
                    rows.append({"condition_id": candidate["condition_id"], "condition": registry[candidate["condition_id"]].public(),
                        "hypothesis": candidate["hypothesis"], "status": "not_run", "verdict": None,
                        "error_code": error or "CONDITION_NOT_RUN", "job_id": None, "elapsed_seconds": None})
            matched = [row["condition_id"] for row in rows if row.get("verdict") == "reproduced"]
            observed = "reproduced" if matched else "not_reproduced_in_tested_conditions" if rows and all(
                row.get("verdict") == "not_reproduced" for row in rows) else "inconclusive"
            if status in {"cancelled", "inconclusive", "failed"}:
                observed = "inconclusive"
            progressed = {row["condition_id"]: sum(a["completed_steps"] for a in (row.get("diagnosis") or {}).get("attempts", [])) for row in rows}
            maximum = max(progressed.values(), default=0)
            report = {"schema_version": "conditions-1", "job_kind": "condition_matrix", "job_id": job_id,
                "program_id": request.program_id, "report_id": request.report_id, "status": status,
                "verdict": observed, "selection": selection, "conditions": rows,
                "matched_condition_ids": matched,
                "most_progressed_condition_ids": [key for key, value in progressed.items() if maximum and value == maximum],
                "progress_basis": "completed_steps_only_not_symptom_similarity",
                "coverage": {"registered": len(registry), "selected": len(selected), "finished": len(completed_ids),
                    "not_selected_ids": [key for key in registry if key not in {c['condition_id'] for c in selected}]},
                "elapsed_seconds": round(time.monotonic() - started, 3), "model_calls": budget.snapshot(),
                "error_code": error, "root_cause_confirmed": False,
                "limitations": ["일치 조건은 각 조건의 두 독립 실행에서 증상을 관찰했다는 뜻입니다.",
                    "등록되지 않거나 선택되지 않은 조건은 검사하지 않았습니다.",
                    "여러 조건을 검사할 때는 정상 작업 전체 기준의 오탐률도 별도로 측정해야 합니다.",
                    "완료 단계가 많다는 이유로 신고 증상과 의미적으로 유사한 사례라고 판단하지 않습니다."]}
            result = ConditionsResult(job_id, request.report_id, request.program_id, status, report)
            self.store.write_json(job_id, "result.json", asdict(result))
            self.store.write(job_id, "report.md", _render(report).encode())
            return result

        try:
            async with asyncio.timeout(self.limits.max_runtime_seconds):
                if select:
                    schema = _object(status=_enum("ready", "needs_info"),
                        candidates=_array(_object(condition_id=_enum(*registry), hypothesis=_text(600)), self.limits.max_conditions),
                        questions=_array(_text(600), 5))
                    calls = _ModelCalls(budget.wrap(self.model, "condition_selection"), self.qa_limits, guard)
                    selection = await calls.invoke("condition_selection", _SELECT,
                        {"report": request.text(), "registered_conditions": [c.public() for c in self.conditions],
                         "max_conditions": self.limits.max_conditions}, schema,
                        (request.screenshot,) if request.screenshot else ())
                    selected = selection["candidates"]
                    if len({c["condition_id"] for c in selected}) != len(selected):
                        raise ModelError("CONDITION_SELECTION_INVALID")
                    if selection["status"] == "needs_info" or selection["questions"] or not selected:
                        selected = []
                        return finish("needs_info")
                else:
                    selected = [{"condition_id": c.id, "hypothesis": "호스트가 등록한 조건을 순서대로 비교합니다."}
                                for c in self.conditions[:self.limits.max_conditions]]
                    selection = {"status": "ready", "candidates": selected, "questions": []}
                self.store.write_json(job_id, "selection.json", selection)
                for candidate in selected:
                    guard()
                    budget.check()
                    condition = registry[candidate["condition_id"]]
                    remaining = max(1, int(self.limits.max_runtime_seconds - (time.monotonic() - started)))
                    service = QAService(budget.wrap(self.model, "primary"), self.policy, self.store,
                        reviewer=budget.wrap(self.reviewer, "critic") if self.reviewer is not None else None,
                        options=condition.options, limits=replace(self.qa_limits, max_runtime_seconds=min(self.qa_limits.max_runtime_seconds, remaining)),
                        browser_limits=self.browser_limits)

                    reset_condition = bind_condition_reset(self.policy, reset, condition.id)

                    before = time.monotonic()
                    row = {"condition_id": condition.id, "condition": condition.public(),
                        "hypothesis": candidate["hypothesis"], "status": "running", "verdict": None,
                        "error_code": None, "job_id": None, "elapsed_seconds": None}

                    def created(child_id):
                        row["job_id"] = child_id
                        rows.append(row)
                        self.store.event(job_id, "condition_started", {"condition_id": condition.id, "job_id": child_id})
                        self.store.write_json(job_id, "conditions.json", rows)

                    result = await service.run(replace(request, entry_url=condition.entry_url or request.entry_url),
                        reset=reset_condition, is_authorized=is_authorized, cancelled=cancelled, _parent=job_id,
                        _job_created=created, live=live_factory(condition.id) if live_factory else None)
                    update_row(row, asdict(result))
                    row["elapsed_seconds"] = round(time.monotonic() - before, 3)
                    self.store.event(job_id, "condition_finished", {"condition_id": condition.id, "status": result.status})
                    self.store.write_json(job_id, "conditions.json", rows)
                    if result.error_code in {"MODEL_RATE_LIMITED", "MODEL_AUTH_FAILED", "AUTHORIZATION_REVOKED", "CANCELLED"} or (
                        result.error_code and result.error_code.startswith("CONDITION_")):
                        error = result.error_code
                        return finish("cancelled" if error == "CANCELLED" else "inconclusive")
                return finish("completed")
        except asyncio.CancelledError:
            cancelled.set()
            error = "CANCELLED"
            finish("cancelled")
            raise
        except TimeoutError:
            cancelled.set()
            error = "CONDITION_RUNTIME_LIMIT"
            return finish("inconclusive")
        except (ModelError, PolicyError) as exc:
            error = exc.code
            return finish("cancelled" if error == "CANCELLED" else "inconclusive")

    async def retest(self, matrix_job_id, condition_id, *, reset, is_authorized, cancelled=None, live=None):
        try:
            allowed = is_authorized()
        except Exception:
            raise PolicyError("AUTHORIZATION_UNAVAILABLE") from None
        if allowed is not True:
            raise PolicyError("AUTHORIZATION_REVOKED")
        parent = self.store.read_json(matrix_job_id, "result.json")
        if parent["program_id"] != self.policy.program_id or parent["report"].get("job_kind") != "condition_matrix":
            raise ValueError("invalid matrix identity")
        condition = next((c for c in self.conditions if c.id == condition_id), None)
        row = next((r for r in parent["report"]["conditions"] if r["condition_id"] == condition_id), None)
        if condition is None or row is None or row.get("verdict") != "reproduced":
            raise ValueError("a reproduced registered condition is required")
        child = self.store.read_json(row["job_id"], "result.json")
        if child["report_id"] != parent["report_id"] or child["program_id"] != parent["program_id"]:
            raise ValueError("condition child identity mismatch")
        service = QAService(self.model, self.policy, self.store, reviewer=self.reviewer,
                            options=condition.options, limits=self.qa_limits, browser_limits=self.browser_limits)

        reset_condition = bind_condition_reset(self.policy, reset, condition.id)

        return await service.retest(row["job_id"], reset=reset_condition, is_authorized=is_authorized, cancelled=cancelled, live=live)


def _render(report):
    lines = ['# 조건별 QA 검증', '', '- 판정: ' + escape(report['verdict']),
             '- 일치 조건: ' + escape(', '.join(report['matched_condition_ids']) or '없음'), '',
             '| 조건 | 상태 | 재현 판정 | 소요(초) |', '|---|---|---|---|']
    details = []
    for row in report['conditions']:
        lines.append('| ' + ' | '.join(escape(value) for value in (row['condition']['label'], row['status'],
                     row.get('verdict') or '미확인', row.get('elapsed_seconds'))) + ' |')
        if row.get('report_path') and re.fullmatch(r'\.\./[a-f0-9]{32}/report\.md', row['report_path']):
            details.append(f"[{escape(row['condition_id'])} 상세 결과]({row['report_path']})")
    lines += ['', *details]
    if (report.get('selection') or {}).get('questions'):
        lines += ['', '## 추가 확인', ''] + ['- ' + escape(q) for q in report['selection']['questions']]
    lines += ['', '## 해석 범위', ''] + ['- ' + escape(x) for x in report['limitations']]
    return '\n'.join(lines) + '\n'
