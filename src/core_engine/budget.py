"""Per-run request limits adapted from IWANTGOHOME research_policy.RequestBudget."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import threading
import time
from typing import Callable, Iterator

from .policy import PolicyError

RATE_WINDOW_SECONDS = 60.0  # the per-minute window; module-level so tests can shorten it


@dataclass(frozen=True)
class BudgetLimits:
    max_requests_per_minute: int = 20
    max_concurrent_requests: int = 2
    max_total_requests: int = 60
    max_download_bytes: int = 8 * 1024 * 1024
    max_response_bytes: int = 2 * 1024 * 1024
    max_runtime_seconds: int = 45
    request_timeout_seconds: int = 5

    def __post_init__(self) -> None:
        ceilings = {
            "max_requests_per_minute": 120,
            "max_concurrent_requests": 4,
            "max_total_requests": 300,
            "max_download_bytes": 32 * 1024 * 1024,
            "max_response_bytes": 8 * 1024 * 1024,
            "max_runtime_seconds": 120,
            "request_timeout_seconds": 30,
        }
        for name, maximum in ceilings.items():
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise PolicyError("INVALID_BUDGET")
        if self.max_response_bytes > self.max_download_bytes:
            raise PolicyError("INVALID_BUDGET")


class RequestBudget:
    def __init__(self, limits: BudgetLimits, *, clock: Callable[[], float] = time.monotonic):
        self.limits = limits
        self.clock = clock
        self.started = clock()
        self.total = 0
        self.active = 0
        self.downloaded = 0
        self.recent: deque[float] = deque()
        self.rate_waiters = 0
        self.rate_wait_started = 0.0
        self.rate_waited = 0.0
        self.lock = threading.Lock()
        self.available = threading.Condition(self.lock)

    def check_runtime(self) -> None:
        if self.clock() - self.started >= self.limits.max_runtime_seconds:
            raise PolicyError("RUNTIME_LIMIT")

    def remaining_seconds(self) -> float:
        self.check_runtime()
        return max(0.001, self.limits.max_runtime_seconds - (self.clock() - self.started))

    def rate_waited_seconds(self) -> float:
        """Wall time during which at least one request was queued for the per-minute window."""
        with self.lock:
            return self.rate_waited + (self.clock() - self.rate_wait_started if self.rate_waiters else 0.0)

    def _rate_wait(self, waiting: bool) -> None:
        # Caller holds the lock. Overlapping waiters count once.
        if waiting:
            if not self.rate_waiters:
                self.rate_wait_started = self.clock()
            self.rate_waiters += 1
        else:
            self.rate_waiters -= 1
            if not self.rate_waiters:
                self.rate_waited += self.clock() - self.rate_wait_started

    @contextmanager
    def request(self, *, wait: bool = False, check: Callable[[], None] | None = None,
                counted: bool = True) -> Iterator[None]:
        """Admit one request. counted=False (same-origin static assets) skips the per-minute and
        total request limits; concurrency, runtime and byte limits still apply."""
        waiting = False
        try:
            while True:
                # Host callbacks run outside the budget lock. Queued requests must still
                # obey cancellation, authorization, runtime, rate and total limits.
                if check:
                    check()
                with self.available:
                    now = self.clock()
                    if now - self.started >= self.limits.max_runtime_seconds:
                        raise PolicyError("RUNTIME_LIMIT")
                    while self.recent and self.recent[0] <= now - RATE_WINDOW_SECONDS:
                        self.recent.popleft()
                    if counted and self.total >= self.limits.max_total_requests:
                        raise PolicyError("REQUEST_LIMIT")
                    deadline = self.started + self.limits.max_runtime_seconds
                    if counted and len(self.recent) >= self.limits.max_requests_per_minute:
                        # A full per-minute window is a pacing delay, not a failure, when the oldest
                        # request leaves the window before the runtime ends. Total stays a hard limit.
                        release = self.recent[0] + RATE_WINDOW_SECONDS
                        if not wait or release >= deadline:
                            raise PolicyError("RATE_LIMIT")
                        if not waiting:
                            waiting = True
                            self._rate_wait(True)
                        self.available.wait(timeout=min(0.05, release - now))
                        continue
                    if waiting:
                        waiting = False
                        self._rate_wait(False)
                    if self.active < self.limits.max_concurrent_requests:
                        if counted:
                            self.total += 1
                            self.recent.append(now)
                        self.active += 1
                        break
                    if not wait:
                        raise PolicyError("CONCURRENCY_LIMIT")
                    self.available.wait(timeout=min(0.05, deadline - now))
        finally:
            if waiting:
                with self.available:
                    self._rate_wait(False)
        try:
            yield
        finally:
            with self.available:
                self.active -= 1
                self.available.notify()

    def received(self, amount: int) -> None:
        if type(amount) is not int or amount < 0:
            raise PolicyError("INVALID_BYTE_COUNT")
        with self.lock:
            if self.downloaded + amount > self.limits.max_download_bytes:
                raise PolicyError("DOWNLOAD_LIMIT")
            self.downloaded += amount

    def check_response_size(self, amount: int) -> None:
        if type(amount) is not int or amount < 0:
            raise PolicyError("INVALID_BYTE_COUNT")
        if amount > self.limits.max_response_bytes:
            raise PolicyError("RESPONSE_LIMIT")
