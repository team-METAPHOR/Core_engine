"""Core engine primitives for authorized web bug validation."""

from .budget import BudgetLimits, RequestBudget
from .browser import BrowserRun, BrowserRunner, Evidence
from .plan import BrowserPlan, Step, TextCheck
from .policy import PolicyError, ResolvedTarget, RunPolicy, canonical_url
from .verdict import AttemptResult, Verdict, judge_attempts

__all__ = [
    "AttemptResult",
    "BudgetLimits",
    "BrowserPlan",
    "BrowserRun",
    "BrowserRunner",
    "Evidence",
    "PolicyError",
    "RequestBudget",
    "ResolvedTarget",
    "RunPolicy",
    "Step",
    "TextCheck",
    "Verdict",
    "canonical_url",
    "judge_attempts",
]
