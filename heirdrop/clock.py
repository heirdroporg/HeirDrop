"""Injectable clock and UTC timestamp helpers."""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
DAY = timedelta(days=1)


def parse_ts(text: str) -> datetime:
    """Parse an ISO 8601 UTC timestamp like 2026-01-02T03:04:05Z."""
    if not isinstance(text, str):
        raise TypeError(f"timestamp is not a string: {text!r}")
    dt = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {text!r}")
    return dt.astimezone(UTC)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeClock:
    """Test clock. sleep() advances time instead of waiting."""

    def __init__(self, start: datetime):
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def advance(self, **kwargs) -> None:
        self._now += timedelta(**kwargs)

    def set(self, dt: datetime) -> None:
        self._now = dt.astimezone(UTC)
