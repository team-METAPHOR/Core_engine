"""Small, validated browser action vocabulary; never accepts executable code."""

from dataclasses import dataclass
from typing import Literal

from .policy import RunPolicy
from .checks import Check


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
    action: Literal["navigate", "reload", "back", "click", "fill", "assert_text"]
    target: str
    value: str | None = None

    def __post_init__(self) -> None:
        if self.action not in {"navigate", "reload", "back", "click", "fill", "assert_text"}:
            raise ValueError("unsupported action")
        maximum = 2048 if self.action == "navigate" else 256
        minimum = 0 if self.action in {"reload", "back"} else 1
        maximum = 0 if minimum == 0 else maximum
        if not isinstance(self.target, str) or not minimum <= len(self.target) <= maximum:
            raise ValueError("invalid action target")
        if self.action in {"fill", "assert_text"}:
            if not isinstance(self.value, str) or len(self.value) > 2000:
                raise ValueError("action requires a bounded value")
        elif self.value is not None:
            raise ValueError("action does not accept a value")


@dataclass(frozen=True)
class BrowserPlan:
    steps: tuple[Step, ...]
    ready: TextCheck | Check
    symptom: TextCheck | Check
    step_timeout_ms: int = 3000
    expected: TextCheck | Check | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.steps, tuple) or not 1 <= len(self.steps) <= 20:
            raise ValueError("plan requires 1..20 immutable steps")
        if not all(isinstance(step, Step) for step in self.steps) or self.steps[0].action != "navigate":
            raise ValueError("plan must start with navigation")
        if not isinstance(self.ready, (TextCheck, Check)) or not isinstance(self.symptom, (TextCheck, Check)):
            raise ValueError("readiness and symptom checks are required")
        if self.expected is not None and not isinstance(self.expected, (TextCheck, Check)):
            raise ValueError("invalid expected check")
        if self.expected == self.symptom:
            raise ValueError("expected and symptom checks must differ")
        if type(self.step_timeout_ms) is not int or not 100 <= self.step_timeout_ms <= 5000:
            raise ValueError("step timeout must be 100..5000 ms")

    def validate_scope(self, policy: RunPolicy) -> None:
        for step in self.steps:
            if step.action == "navigate":
                policy.check_url(step.target)
        for check in (self.ready, self.expected, self.symptom):
            if isinstance(check, Check) and check.kind == "response_status":
                policy.check_url(policy.base_url.rstrip("/") + check.selector, check.other_selector)
            if isinstance(check, Check) and check.kind == "url_path":
                policy.check_url(policy.base_url.rstrip("/") + check.value)
