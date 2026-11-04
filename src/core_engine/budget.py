"""Per-run request limits adapted from IWANTGOHOME research_policy.RequestBudget."""

from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass
import threading
import time
from typing import Callable, Iterator

from .policy import PolicyError


@dataclass(frozen=True)
class BudgetLimits:
    max_requests_per_minute: int = 20
    max_concurrent_requests: int = 2
    max_total_requests: int = 60
    max_download_bytes: int = 8 * 1024 * 1024
    max_response_bytes: int = 2 * 1024 * 1024
    max_runtime_seconds: int = 45

    def __post_init__(self) -> None:
        ceilings = {
            "max_requests_per_minute": 120,
            "max_concurrent_requests": 4,
            "max_total_requests": 300,
            "max_download_bytes": 32 * 1024 * 1024,
            "max_response_bytes": 8 * 1024 * 1024,
            "max_runtime_seconds": 120,
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
        self.lock = threading.Lock()

    def check_runtime(self) -> None:
        if self.clock() - self.started >= self.limits.max_runtime_seconds:
            raise PolicyError("RUNTIME_LIMIT")

    @contextmanager
    def request(self) -> Iterator[None]:
        with self.lock:
            now = self.clock()
            if now - self.started >= self.limits.max_runtime_seconds:
                raise PolicyError("RUNTIME_LIMIT")
            while self.recent and self.recent[0] <= now - 60:
                self.recent.popleft()
            if self.total >= self.limits.max_total_requests:
                raise PolicyError("REQUEST_LIMIT")
            if len(self.recent) >= self.limits.max_requests_per_minute:
                raise PolicyError("RATE_LIMIT")
            if self.active >= self.limits.max_concurrent_requests:
                raise PolicyError("CONCURRENCY_LIMIT")
            self.total += 1
            self.active += 1
            self.recent.append(now)
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1

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
