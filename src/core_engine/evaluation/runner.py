"""Bounded sequential evaluation with incremental, private result artifacts."""

import argparse
import asyncio
import copy
import csv
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
from io import StringIO
import json
import mmap
import os
from pathlib import Path
import random
import subprocess
import sys
import threading
import time
import traceback
from uuid import uuid4

from core_engine import BrowserOptions, BrowserRunner, GroqModel, JobStore, ModelError, OpenAIModel, PolicyError, QARequest, QAService, Recorder
from core_engine.qa_schema import decode_plan
from . import SUITE_VERSION
from .cases import CASES, reference_plan
from .fixture_model import FixtureModel
from .grading import grade, render_summary, summarize, verdict
from .site import FixtureSite, fixture_site
from . import transfer


async def reset(_):
    # Only synthetic per-page in-memory sessions are used; a fresh context gets a fresh session.
    return True


def write_private(path, text):
    temporary = path.with_name(uuid4().hex + ".tmp")
    try:
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_private(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


class CallBudget:
    def __init__(self, maximum):
        self.maximum, self.calls, self.lock = maximum, [], threading.Lock()

    @property
    def exhausted(self):
        with self.lock:
            return len(self.calls) >= self.maximum

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.calls)

    def wrap(self, model, trial_id, role="primary"):
        budget = self

        class TrackedModel:
            def generate(self, **arguments):
                with budget.lock:
                    if len(budget.calls) >= budget.maximum:
                        raise PolicyError("MODEL_CALL_LIMIT")
                    record = {"trial_id": trial_id, "role": role, "status": "started", "model": None, "usage": None}
                    budget.calls.append(record)
                started = time.monotonic()
                try:
                    generated = model.generate(**arguments)
                    with budget.lock:
                        record.update(status="completed", model=generated.model,
                                      usage=asdict(generated.usage) if generated.usage is not None else None)
                    return generated
                except Exception as exc:
                    with budget.lock:
                        record["status"] = exc.code if isinstance(exc, ModelError) else "PROVIDER_EXCEPTION"
                    raise
                finally:
                    with budget.lock:
                        record["elapsed_seconds"] = round(time.monotonic() - started, 3)

        return TrackedModel()


class OracleRecorder(Recorder):
    def __init__(self, store, job_id, options, site):
        super().__init__(store, job_id, options)
        self.site, self.observed = site, []

    async def capture(self, page, **kwargs):
        snapshot = await super().capture(page, **kwargs)
        if kwargs["phase"] in {"verdict", "blocked"}:
            truth = await self.site.observe_truth(page)
            self.observed.append({"attempt": kwargs["attempt"], **truth})
        return snapshot


async def capture_source(case, site, store, options, reference_builder=reference_plan):
    site.activate("control" if case.category == "needs_info" else "issue")
    job_id = store.create()
    recorder = Recorder(store, job_id, options)
    runner = BrowserRunner(site.policy(), options=options, recorder=recorder)
    if case.category == "needs_info":
        await runner.inspect(site.url, reset=reset, is_authorized=lambda: True)
    else:
        result = await runner.run(reference_builder(case.family, site.url), reset=reset, is_authorized=lambda: True)
        if result.verdict.value != "reproduced":
            raise ValueError("source fixture did not produce the intended symptom")
    return recorder.image(recorder.snapshots[-1])


async def independently_replay(case, result, site, store, options):
    if case.category not in {"bug", "normal"} or verdict(result) not in {"reproduced", "not_reproduced"}:
        return {}
    plan = decode_plan(result["report"]["scenario"], timeout_ms=options.step_timeout_ms)
    job_id = store.create()
    recorder = OracleRecorder(store, job_id, options, site)
    replay = await BrowserRunner(site.policy(), options=options, recorder=recorder).run(
        plan, reset=reset, is_authorized=lambda: True)
    output = {"job_id": job_id, "replay_completed": all(attempt.completed for attempt in replay.attempts),
              "attempts": recorder.observed}
    store.write_json(job_id, "oracle.json", output)
    return output


def blank_row(case, repeat, kind="initial"):
    return {"case_id": case.id, "family": case.family, "category": case.category,
            "repeat": repeat, "kind": kind, "status": "not_run", "verdict": None,
            "passed": False, "reason": "NOT_STARTED", "error_code": None,
            "elapsed_seconds": 0, "report_path": None, "oracle_path": None, "regression": None}


async def evaluate_job(case, site, store, options, model, *, request=None, baseline=None, reviewer=None):
    service = QAService(model, site.policy(), store, options=options, reviewer=reviewer)
    started = time.monotonic()
    if baseline:
        result = await service.retest(baseline, reset=reset, is_authorized=lambda: True)
    else:
        result = await service.run(request, reset=reset, is_authorized=lambda: True)
    elapsed = round(time.monotonic() - started, 3)
    data = asdict(result)
    requests = list(site.requests)
    oracle = await independently_replay(case, data, site, store, options)
    scored = grade(case, data, oracle, requests)
    return data, oracle, {"status": result.status, "verdict": verdict(data), **scored,
        "error_code": result.error_code, "elapsed_seconds": elapsed, "regression": result.report["regression"],
        "engine_job_id": result.job_id, "plan_sha256": result.report["plan_sha256"],
        "oracle": oracle, "questions": list(result.questions), "analysis_available": result.report["analysis"] is not None}


def persist(root, rows, budget, *, live, retest_fixed):
    calls = budget.snapshot()
    summary = summarize(rows, calls, mode="live" if live else "fixture", retest_fixed=retest_fixed)
    write_json(root / "results.json", rows)
    write_json(root / "model_calls.json", calls)
    write_json(root / "summary.json", summary)
    write_private(root / "summary.md", render_summary(summary, rows))
    columns = ["case_id", "family", "category", "repeat", "kind", "status", "verdict", "passed", "reason",
               "error_code", "elapsed_seconds", "regression", "analysis_available", "report_path", "oracle_path"]
    csv_text = StringIO(newline="")
    writer = csv.DictWriter(csv_text, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    write_private(root / "results.csv", csv_text.getvalue())
    return summary


def source_metadata():
    source = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    for path in sorted(source.rglob("*.py")):
        digest.update(path.relative_to(source).as_posix().encode())
        # Memory-mapped reads also work when macOS file-provider reads time out.
        if path.stat().st_size:
            with path.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as contents:
                digest.update(contents)
    metadata = {"engine_source_sha256": digest.hexdigest(), "python": sys.version.split()[0],
                "playwright": version("playwright"), "suite_version": SUITE_VERSION}
    try:
        metadata["git_commit"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source,
                                                         stderr=subprocess.DEVNULL, text=True).strip()
        metadata["git_dirty"] = bool(subprocess.check_output(["git", "status", "--porcelain"], cwd=source, text=True))
    except (OSError, subprocess.CalledProcessError):
        metadata["git_commit"] = None
    return metadata


async def run_suite(args, provider=None, reviewer=None):
    critic_mode = getattr(args, "critic", "none")
    if critic_mode not in {"none", "self", "groq"}:
        raise ValueError("unknown critic mode")
    if critic_mode != "none" and (not args.live or reviewer is None):
        raise ValueError("critic evaluation requires live mode and an explicit reviewer")
    if critic_mode == "none" and reviewer is not None:
        raise ValueError("reviewer supplied while critic mode is disabled")
    suite = getattr(args, "suite", "core")
    if suite not in {"core", "transfer"}:
        raise ValueError("unknown evaluation suite")
    cases, suite_version, site_factory, reference_builder = (
        (CASES, SUITE_VERSION, FixtureSite, reference_plan) if suite == "core" else
        (transfer.CASES, transfer.SUITE_VERSION, transfer.TransferSite, transfer.reference_plan))
    if args.case and not set(args.case).issubset({case.id for case in cases}):
        raise ValueError("selected cases do not belong to this suite")
    selected = [case for case in cases if not args.case or case.id in args.case]
    tasks = [(case, repeat) for repeat in range(1, args.repeats + 1) for case in selected]
    random.Random(args.seed).shuffle(tasks)
    root = Path(args.output).resolve() / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid4().hex[:8])
    root.mkdir(parents=True, mode=0o700)
    rows, indexes = [], {}
    for case, repeat in tasks:
        indexes[(case.id, repeat, "initial")] = len(rows)
        rows.append(blank_row(case, repeat))
        if args.retest_fixed and case.category == "bug":
            indexes[(case.id, repeat, "retest")] = len(rows)
            rows.append(blank_row(case, repeat, "retest"))
    budget = CallBudget(args.max_model_calls)
    manifest = {**source_metadata(), "suite_version": suite_version, "suite": suite,
        "mode": "live" if args.live else "fixture",
        "model": getattr(provider, "model", "offline-fixture"), "seed": args.seed,
        "critic": critic_mode, "critic_model": getattr(reviewer, "model", None),
        "repeats": args.repeats, "max_model_calls": args.max_model_calls, "retest_fixed": args.retest_fixed,
        "cases": [asdict(case) for case in selected], "order": [[case.id, repeat] for case, repeat in tasks],
        "labels_sent_to_model": False, "input_policy": "Identical reporter image/text for each bug/control pair; neutral random target paths.",
        "dataset_use": "public synthetic development suite, not held-out evaluation"}
    write_json(root / "manifest.json", manifest)
    print(json.dumps({"output": str(root), "mode": manifest["mode"], "planned_trials": len(tasks)}, ensure_ascii=False), flush=True)
    persist(root, rows, budget, live=args.live, retest_fixed=args.retest_fixed)
    try:
        for case, repeat in tasks:
            index = indexes[(case.id, repeat, "initial")]
            active_index = index
            trial_id = f"{case.id}_{repeat}"
            if budget.exhausted:
                rows[index]["reason"] = "EVAL_MODEL_CALL_LIMIT"
                continue
            try:
                with fixture_site(case.family, site_factory=site_factory) as site:
                    store = JobStore(root / "trials" / trial_id)
                    options = BrowserOptions(app_version="evaluation-1")
                    image = await capture_source(case, site, store, options, reference_builder)
                    site.activate(case.variant)
                    request = QARequest(uuid4().hex, "qa-evaluation", site.url, image, **case.report_text())
                    base_model = provider if args.live else FixtureModel(case, site.url, reference_builder)
                    data, oracle, record = await evaluate_job(case, site, store, options,
                        budget.wrap(base_model, trial_id), request=request,
                        reviewer=budget.wrap(reviewer, trial_id, "critic") if reviewer is not None else None)
                    rows[index].update(record)
                    rows[index]["report_path"] = (store.root / data["job_id"] / "report.md").relative_to(root).as_posix()
                    if oracle:
                        rows[index]["oracle_path"] = (store.root / oracle["job_id"] / "oracle.json").relative_to(root).as_posix()
                    if args.retest_fixed and case.category == "bug":
                        retest_index = indexes[(case.id, repeat, "retest")]
                        if not rows[index]["passed"]:
                            rows[retest_index].update(status="not_applicable", reason="BASELINE_NOT_CONFIRMED")
                        elif budget.exhausted:
                            rows[retest_index]["reason"] = "EVAL_MODEL_CALL_LIMIT"
                        else:
                            active_index = retest_index
                            site.activate("control")
                            fixed_case = replace(case, category="normal", variant="control")
                            after, fixed_oracle, fixed_record = await evaluate_job(
                                fixed_case, site, store, replace(options, app_version="evaluation-2"),
                                budget.wrap(base_model, trial_id + "_retest"), baseline=data["job_id"],
                                reviewer=budget.wrap(reviewer, trial_id + "_retest", "critic") if reviewer is not None else None)
                            same = data["report"]["plan_sha256"] == after["report"]["plan_sha256"]
                            fixed_record["passed"] &= same and after["report"]["regression"] == "resolved_in_test"
                            if not same:
                                fixed_record["reason"] = "PLAN_CHANGED"
                            elif after["report"]["regression"] != "resolved_in_test":
                                fixed_record["reason"] = "REGRESSION_MISMATCH"
                            rows[retest_index].update(fixed_record)
                            rows[retest_index]["report_path"] = (store.root / after["job_id"] / "report.md").relative_to(root).as_posix()
                            if fixed_oracle:
                                rows[retest_index]["oracle_path"] = (store.root / fixed_oracle["job_id"] / "oracle.json").relative_to(root).as_posix()
            except asyncio.CancelledError:
                rows[active_index].update(status="interrupted", reason="INTERRUPTED")
                raise
            except Exception as exc:
                # Never persist provider/page exception messages or secret-bearing reprs.
                rows[active_index].update(status="harness_error", reason="HARNESS_ERROR", error_code=type(exc).__name__,
                    error_frames=[{"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                                  for frame in traceback.extract_tb(exc.__traceback__)])
                if active_index == index and args.retest_fixed and case.category == "bug":
                    rows[indexes[(case.id, repeat, "retest")]].update(status="not_applicable", reason="BASELINE_NOT_CONFIRMED")
            persist(root, rows, budget, live=args.live, retest_fixed=args.retest_fixed)
            print(json.dumps({key: rows[index][key] for key in ("case_id", "repeat", "status", "verdict", "passed", "reason")}, ensure_ascii=False), flush=True)
    finally:
        summary = persist(root, rows, budget, live=args.live, retest_fixed=args.retest_fixed)
    return root, summary, rows


def main():
    parser = argparse.ArgumentParser(description="소규모 합성 QA 평가. 기본값은 고정 모델 연결 검증입니다.")
    parser.add_argument("--live", action="store_true", help="실제 유료 모델 호출; 환경변수 설정 필요")
    parser.add_argument("--critic", choices=("none", "self", "groq"), default="none",
                        help="실행 전 검토 없음 / 주 모델 자체 검토 / Groq 비판자. --live 필요")
    parser.add_argument("--list", action="store_true", help="평가 사례만 출력")
    parser.add_argument("--suite", choices=("core", "transfer"), default="core", help="기존 12개 또는 추가 8개 개발 사례")
    parser.add_argument("--case", action="append", choices=[case.id for case in (*CASES, *transfer.CASES)], help="여러 번 지정 가능")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--seed", type=int, default=17, help="실행 순서만 고정. 모델의 무작위성은 고정하지 않음")
    parser.add_argument("--output", default=".qa-evals")
    parser.add_argument("--max-model-calls", type=int, default=120, help="평가 전체 공급자 호출 상한; 자동 재시도 없음")
    parser.add_argument("--retest-fixed", action="store_true", help="재현에 성공한 버그를 같은 계획으로 수정 버전에서 추가 검증")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10 or not 1 <= args.max_model_calls <= 1000:
        parser.error("repeats는 1..10, max-model-calls는 1..1000이어야 합니다.")
    cases = CASES if args.suite == "core" else transfer.CASES
    if args.case and not set(args.case).issubset({case.id for case in cases}):
        parser.error("선택한 사례가 해당 suite에 속하지 않습니다.")
    if args.list:
        print(json.dumps([{"id": case.id, "category": case.category} for case in cases], ensure_ascii=False, indent=2))
        return
    if args.critic != "none" and not args.live:
        parser.error("비판자 비교는 --live와 함께 실행하세요. 고정 모델 점수와 혼합하지 않습니다.")
    try:
        provider = OpenAIModel.from_env() if args.live else None
        reviewer = GroqModel.from_env() if args.critic == "groq" else provider if args.critic == "self" else None
    except ValueError as exc:
        parser.error(str(exc))
    try:
        root, summary, rows = asyncio.run(run_suite(args, provider, reviewer))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    print(json.dumps({"summary": str(root / "summary.md"), **summary}, ensure_ascii=False), flush=True)
    raise SystemExit(0 if summary["complete"] and all(row["passed"] for row in rows) else 1)
