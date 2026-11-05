"""Core engine primitives for authorized web bug validation."""

from .budget import BudgetLimits, RequestBudget
from .analysis import AnalysisResult, AnalysisService, Assessment, BugReport, Hypothesis, Solution
from .browser import BrowserRun, BrowserRunner, Evidence
from .model import Generation, JsonModel, ModelError, TokenUsage
from .openai_provider import OpenAIModel
from .groq_provider import GroqModel
from .artifacts import JobStore
from .checks import Check
from .images import Screenshot
from .observation import BrowserOptions, Recorder
from .qa import QALimits, QARequest, QAResult, QAService
from .plan import BrowserPlan, Step, TextCheck
from .policy import PolicyError, ResolvedTarget, RunPolicy, canonical_url
from .verdict import AttemptResult, Verdict, judge_attempts
from .test_export import export_playwright_test
from .conditions import ConditionLimits, ConditionsResult, ConditionsService, TestCondition

__all__ = [
    "BrowserOptions",
    "Check",
    "JobStore",
    "QALimits",
    "QARequest",
    "QAResult",
    "QAService",
    "Recorder",
    "Screenshot",
    "AnalysisResult",
    "AnalysisService",
    "Assessment",
    "AttemptResult",
    "BugReport",
    "BudgetLimits",
    "BrowserPlan",
    "BrowserRun",
    "BrowserRunner",
    "Evidence",
    "Generation",
    "GroqModel",
    "Hypothesis",
    "JsonModel",
    "ModelError",
    "OpenAIModel",
    "PolicyError",
    "RequestBudget",
    "ResolvedTarget",
    "RunPolicy",
    "Step",
    "Solution",
    "TextCheck",
    "TokenUsage",
    "Verdict",
    "canonical_url",
    "judge_attempts",
    "export_playwright_test",
    "ConditionLimits",
    "ConditionsResult",
    "ConditionsService",
    "TestCondition",
]
