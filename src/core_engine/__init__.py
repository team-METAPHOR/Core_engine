"""Core engine primitives for authorized web bug validation."""

from .budget import BudgetLimits, RequestBudget
from .analysis import AnalysisResult, AnalysisService, Assessment, BugReport, Hypothesis, Solution
from .browser import BrowserRun, BrowserRunner, Evidence
from .model import Generation, JsonModel, ModelError, TokenUsage
from .openai_provider import OpenAIModel
from .plan import BrowserPlan, Step, TextCheck
from .policy import PolicyError, ResolvedTarget, RunPolicy, canonical_url
from .verdict import AttemptResult, Verdict, judge_attempts

__all__ = [
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
]
