"""Small, validated browser action vocabulary; never accepts executable code."""

from dataclasses import dataclass
from typing import Literal

from .policy import RunPolicy


@dataclass(frozen=True)
class TextCheck:
    selector: str
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.selector, str) or not 1 <= len(self.selector) <= 256:
            raise ValueError("selector must be a bounded CSS selector")
        if not isinstance(self.text, str) or len(self.text) > 2000:
            raise ValueError("check text must be a bounded string")


@dataclass(frozen=True)
class Step:
    action: Literal["navigate", "click", "fill", "assert_text"]
    target: str
    value: str | None = None

    def __post_init__(self) -> None:
        if self.action not in {"navigate", "click", "fill", "assert_text"}:
            raise ValueError("unsupported action")
        maximum = 2048 if self.action == "navigate" else 256
        if not isinstance(self.target, str) or not 1 <= len(self.target) <= maximum:
            raise ValueError("invalid action target")
        if self.action in {"fill", "assert_text"}:
            if not isinstance(self.value, str) or len(self.value) > 2000:
                raise ValueError("action requires a bounded value")
        elif self.value is not None:
            raise ValueError("action does not accept a value")


@dataclass(frozen=True)
class BrowserPlan:
    steps: tuple[Step, ...]
    ready: TextCheck
    symptom: TextCheck
    step_timeout_ms: int = 3000

    def __post_init__(self) -> None:
        if not isinstance(self.steps, tuple) or not 1 <= len(self.steps) <= 20:
            raise ValueError("plan requires 1..20 immutable steps")
        if not all(isinstance(step, Step) for step in self.steps) or self.steps[0].action != "navigate":
            raise ValueError("plan must start with navigation")
        if not isinstance(self.ready, TextCheck) or not isinstance(self.symptom, TextCheck):
            raise ValueError("readiness and symptom checks are required")
        if type(self.step_timeout_ms) is not int or not 100 <= self.step_timeout_ms <= 5000:
            raise ValueError("step timeout must be 100..5000 ms")

    def validate_scope(self, policy: RunPolicy) -> None:
        for step in self.steps:
            if step.action == "navigate":
                policy.check_url(step.target)
