"""Explicit binding between host-approved writes and a server-state reset adapter."""

from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .policy import PolicyError


@dataclass(frozen=True)
class StateReset:
    adapter_id: str
    callback: Callable[..., Awaitable[bool]] = field(repr=False, compare=False)

    def __post_init__(self):
        if not callable(self.callback):
            raise PolicyError("STATE_RESET_REQUIRED")

    async def __call__(self, *args):
        return await self.callback(*args)


def validate_reset(policy, reset):
    if policy.state_changes and (not isinstance(reset, StateReset)
            or reset.adapter_id != policy.state_changes.reset_adapter_id):
        raise PolicyError("STATE_RESET_REQUIRED")


def bind_condition_reset(policy, reset, condition_id):
    validate_reset(policy, reset)
    async def bound(attempt):
        return await reset(condition_id, attempt)
    return StateReset(reset.adapter_id, bound) if isinstance(reset, StateReset) else bound
