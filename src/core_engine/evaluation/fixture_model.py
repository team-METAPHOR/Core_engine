"""Explicit canned responses for harness verification; never an AI benchmark score."""

import json

from core_engine import Generation, TokenUsage
from core_engine.qa_schema import encode_plan
from .cases import reference_plan


class FixtureModel:
    def __init__(self, case, url):
        self.case, self.url = case, url

    def generate(self, *, instructions, context, schema, images=()):
        fields = schema["properties"]
        if "observed_facts" in fields:
            missing = self.case.category == "needs_info"
            value = {"status": "needs_info" if missing else "ready", "title": "제보 확인",
                "classification": "visual" if self.case.family in {"clipping", "overlap"} else "functional",
                "observed_facts": ["제보 화면이 제공됨"], "hypotheses": [],
                "expected_behavior": "" if missing else self.case.expected_result,
                "expectation_source": "unknown" if missing else "report",
                "symptom_description": self.case.description or "기대 동작 정보가 필요함",
                "reproduction_steps": [], "preconditions": [],
                "questions": ["기대 동작과 사용한 검색어를 알려주세요."] if missing else []}
        elif "plan" in fields:
            value = {"status": "ready", "questions": [], "rationale": "고정 응답으로 실행기 검증",
                     "plan": encode_plan(reference_plan(self.case.family, self.url))}
        elif "target" in fields:
            value = {"status": "stop", "target": "", "reason": "고정 모델은 자동 보정하지 않음", "questions": []}
        else:
            value = {"classification": "functional", "confidence": "low", "severity": "unknown",
                "classification_reason": "고정 응답", "severity_reason": "실제 AI 평가 아님",
                "summary": "실행기 연결 검증용 보고서", "hypotheses": [],
                "solutions": [{"title": "검토", "description": "보고된 현상을 확인하세요.",
                    "source_refs": ["report"], "verification_steps": ["같은 조건에서 다시 확인"], "tradeoffs": []}],
                "missing_information": []}
        return Generation(json.dumps(value, ensure_ascii=False), "offline-fixture", TokenUsage(0, 0))
