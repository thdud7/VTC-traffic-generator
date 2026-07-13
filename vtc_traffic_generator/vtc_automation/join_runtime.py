"""Bounded runtime primitives shared by meeting join state machines."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


class JoinDeadlineExceeded(TimeoutError):
    """Raised when the single meeting-join budget has been exhausted."""


@dataclass(frozen=True)
class JoinDeadline:
    expires_at: float
    started_at: float

    @classmethod
    def after(cls, seconds: float) -> "JoinDeadline":
        now = time.monotonic()
        return cls(now + max(0.0, float(seconds)), now)

    def remaining_sec(self) -> float:
        return max(0.0, self.expires_at - time.monotonic())

    def remaining_ms(self, cap_ms: float | None = None) -> int:
        remaining = int(self.remaining_sec() * 1000)
        if cap_ms is not None:
            remaining = min(remaining, max(0, int(cap_ms)))
        return max(0, remaining)

    def expired(self) -> bool:
        return self.remaining_sec() <= 0

    def elapsed_sec(self) -> float:
        return max(0.0, time.monotonic() - self.started_at)

    def raise_if_expired(self, stage: str) -> None:
        if self.expired():
            raise JoinDeadlineExceeded(f"Webex join deadline expired during {stage}")

    def require_reserve(self, stage: str, reserve_sec: float) -> None:
        """Fail before starting work that cannot finish inside the global budget."""
        reserve_sec = max(0.0, float(reserve_sec))
        remaining = self.remaining_sec()
        if remaining < reserve_sec:
            raise JoinDeadlineExceeded(
                f"Webex join deadline reserve unavailable during {stage}: "
                f"remaining={remaining:.3f}s required={reserve_sec:.3f}s"
            )


async def wait_with_deadline(awaitable, deadline: JoinDeadline, stage: str, cap_sec: float | None = None):
    """Await work within the global budget and always reap a timed-out task."""
    deadline.raise_if_expired(stage)
    timeout = deadline.remaining_sec()
    if cap_sec is not None:
        timeout = min(timeout, max(0.0, float(cap_sec)))
    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.wait_for(task, timeout=timeout)
    except asyncio.TimeoutError:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if deadline.expired():
            raise JoinDeadlineExceeded(f"Webex join deadline expired during {stage}")
        raise
