"""Retry with exponential backoff and an async-safe circuit breaker."""

from __future__ import annotations

import asyncio
import enum
import logging
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, Tuple, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def backoff_delay(attempt: int, base: float, maximum: float, jitter: float = 0.25) -> float:
    """Exponential backoff for attempt >= 1 with +/- jitter (decorrelates retries)."""
    exp = min(maximum, base * (2 ** max(0, attempt - 1)))
    spread = exp * jitter
    return max(0.0, min(maximum, exp + random.uniform(-spread, spread)))


def retry_sync(
    func: Callable[[], T],
    *,
    attempts: int,
    base_delay: float,
    max_delay: float,
    retry_on: Tuple[Type[BaseException], ...],
    description: str,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Call ``func`` until it succeeds or ``attempts`` is exhausted."""
    attempts = max(1, attempts)
    last_exc: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except retry_on as exc:
            last_exc = exc
            if attempt >= attempts:
                break
            delay = backoff_delay(attempt, base_delay, max_delay)
            logger.warning(
                "%s failed (attempt %d/%d): %s; retrying in %.1fs",
                description, attempt, attempts, exc, delay,
            )
            sleep(delay)
    assert last_exc is not None
    raise last_exc


class CircuitState(str, enum.Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(Exception):
    """Raised when a call is rejected because the breaker is open."""

    def __init__(self, name: str, retry_after: float):
        super().__init__(f"Circuit '{name}' is open; retry in {retry_after:.0f}s")
        self.name = name
        self.retry_after = retry_after


@dataclass
class CircuitSnapshot:
    name: str
    state: CircuitState
    consecutive_failures: int
    opened_at: Optional[float]


class CircuitBreaker:
    """Classic three-state breaker.

    CLOSED: calls flow; consecutive failures are counted.
    OPEN: calls fail fast until ``recovery_timeout`` has elapsed.
    HALF_OPEN: exactly one trial call is allowed; success closes the breaker,
    failure re-opens it for another full timeout.
    """

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be >= 1")
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self._clock = clock
        self._state = CircuitState.CLOSED
        self._failures = 0
        self._opened_at: Optional[float] = None
        self._trial_in_flight = False
        self._lock = asyncio.Lock()

    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN and self._opened_at is not None:
            if self._clock() - self._opened_at >= self.recovery_timeout:
                return CircuitState.HALF_OPEN
        return self._state

    def snapshot(self) -> CircuitSnapshot:
        return CircuitSnapshot(self.name, self.state, self._failures, self._opened_at)

    def is_call_permitted(self) -> bool:
        current = self.state
        if current == CircuitState.CLOSED:
            return True
        if current == CircuitState.HALF_OPEN:
            return not self._trial_in_flight
        return False

    async def before_call(self) -> None:
        async with self._lock:
            current = self.state
            if current == CircuitState.CLOSED:
                return
            if current == CircuitState.HALF_OPEN and not self._trial_in_flight:
                self._state = CircuitState.HALF_OPEN
                self._trial_in_flight = True
                logger.info("Circuit '%s' half-open: allowing trial request", self.name)
                return
            remaining = self.recovery_timeout
            if self._opened_at is not None:
                remaining = max(0.0, self.recovery_timeout - (self._clock() - self._opened_at))
            raise CircuitOpenError(self.name, remaining)

    async def record_success(self) -> None:
        async with self._lock:
            if self._state != CircuitState.CLOSED:
                logger.info("Circuit '%s' closed: upstream recovered", self.name)
            self._state = CircuitState.CLOSED
            self._failures = 0
            self._opened_at = None
            self._trial_in_flight = False

    async def record_failure(self) -> None:
        async with self._lock:
            self._failures += 1
            trial_failed = self._trial_in_flight
            self._trial_in_flight = False
            if trial_failed or self._failures >= self.failure_threshold:
                if self._state != CircuitState.OPEN or trial_failed:
                    logger.error(
                        "Circuit '%s' opened after %d consecutive failures",
                        self.name, self._failures,
                    )
                self._state = CircuitState.OPEN
                self._opened_at = self._clock()

    async def call(self, func: Callable[[], Awaitable[T]]) -> T:
        await self.before_call()
        try:
            result = await func()
        except Exception:
            await self.record_failure()
            raise
        await self.record_success()
        return result
