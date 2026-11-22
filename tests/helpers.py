from __future__ import annotations

from datetime import datetime, timedelta

from heirdrop.clock import UTC
from heirdrop.config import Timing
from heirdrop.logic import Facts

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

DEFAULT_TIMING = Timing(
    owner_period=timedelta(days=74),
    warning_window=timedelta(days=14),
    owner_grace=timedelta(days=16),
    wife_window=timedelta(days=30),
    cold_start_hold=timedelta(hours=24),
    cold_start_gap_threshold=timedelta(hours=6),
    tick_interval=timedelta(seconds=60),
    confirmations_final=6,
    max_clock_skew=timedelta(hours=4),
    max_alert_wait=timedelta(hours=24),
)


def day(n: float) -> datetime:
    return T0 + timedelta(days=n)


def armed_facts(owner_day: float = 0, wife_day: float | None = None) -> Facts:
    """Facts for a service armed at T0 with owner check-in at owner_day."""
    f = Facts(armed_at=T0, owner_checkin=day(owner_day))
    if wife_day is not None:
        f.wife_checkin = day(wife_day)
    return f


def owned_by_non_root(path) -> None:
    """Make the non-root permission rule apply when the tests run as root:
    a root-owned file may be group-readable (config.permission_problem)."""
    import os
    if os.geteuid() == 0:
        os.chown(path, 65534, -1)  # nobody; root still reads and writes it
