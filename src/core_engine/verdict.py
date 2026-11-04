"""Evidence-gated verdict for two independent validation attempts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


VerdictValue = Literal["reproduced", "not_reproduced", "inconclusive"]


@dataclass(frozen=True)
class AttemptResult:
    completed: bool
    symptom_observed: bool | None
    evidence_ids: tuple[str, ...] = ()
    blocker: str | None = None

    def __post_init__(self) -> None:
        if type(self.completed) is not bool:
            raise ValueError("completed must be bool")
        if self.completed:
            if type(self.symptom_observed) is not bool or not self.evidence_ids or self.blocker is not None:
                raise ValueError("completed attempt needs a symptom observation and evidence")
        elif self.symptom_observed is not None or not self.blocker:
            raise ValueError("blocked attempt needs a reason and no symptom conclusion")
        if any(not isinstance(item, str) or not item for item in self.evidence_ids):
            raise ValueError("evidence IDs must be nonempty strings")


@dataclass(frozen=True)
class Verdict:
    value: VerdictValue
    reason_code: str
    evidence_ids: tuple[str, ...]


def judge_attempts(attempts: tuple[AttemptResult, AttemptResult]) -> Verdict:
    """Do not infer a bug from one run, a transport error, or a model claim."""
    if len(attempts) != 2 or any(not isinstance(attempt, AttemptResult) for attempt in attempts):
        raise ValueError("exactly two attempts are required")
    evidence_ids = tuple(dict.fromkeys(item for attempt in attempts for item in attempt.evidence_ids))
    if not all(attempt.completed for attempt in attempts):
        return Verdict("inconclusive", "ATTEMPT_BLOCKED", evidence_ids)
    observations = tuple(attempt.symptom_observed for attempt in attempts)
    if observations == (True, True):
        return Verdict("reproduced", "SYMPTOM_OBSERVED_TWICE", evidence_ids)
    if observations == (False, False):
        return Verdict("not_reproduced", "SYMPTOM_NOT_OBSERVED_TWICE", evidence_ids)
    return Verdict("inconclusive", "ATTEMPTS_DISAGREE", evidence_ids)
