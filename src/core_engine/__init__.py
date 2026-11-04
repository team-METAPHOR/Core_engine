"""Core engine primitives for authorized web bug validation."""

from .budget import BudgetLimits, RequestBudget
from .policy import PolicyError, ResolvedTarget, RunPolicy, canonical_url
from .verdict import AttemptResult, Verdict, judge_attempts

__all__ = [
    "AttemptResult",
    "BudgetLimits",
    "PolicyError",
    "RequestBudget",
    "ResolvedTarget",
    "RunPolicy",
    "Verdict",
    "canonical_url",
    "judge_attempts",
]
