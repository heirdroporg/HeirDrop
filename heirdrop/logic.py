"""Pure derivation of the current situation from facts on disk and the clock.

Nothing here touches disk, the network or the real clock. Given the facts the
StateStore read and the current time, derive() returns deadlines, the phase,
what decision (if any) is due, why acting is blocked, and the alerts that
should exist right now. Every alert has a deterministic id, so the service can
create it exclusively and never send a duplicate after a restart.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .clock import DAY, fmt_ts
from .config import PAIRS, Timing


@dataclass(frozen=True)
class Pause:
    start: datetime | None  # None: there was no previous tick (first start)
    end: datetime
    hold_until: datetime | None


@dataclass(frozen=True)
class Decision:
    choice: str  # "W" or "F"
    at: datetime


@dataclass(frozen=True)
class Broadcast:
    name: str
    txid: str
    at: datetime
    dry_run: bool
    blockhash: str | None = None
    block_height: int | None = None


@dataclass
class Facts:
    armed_at: datetime | None = None
    owner_checkin: datetime | None = None
    wife_checkin: datetime | None = None
    last_tick: datetime | None = None
    pauses: list[Pause] = field(default_factory=list)
    decision: Decision | None = None
    broadcasts: dict[str, Broadcast] = field(default_factory=dict)
    final_at: datetime | None = None
    alert_accepted: dict[str, datetime | None] = field(default_factory=dict)  # alert id -> accepted at


@dataclass(frozen=True)
class Alert:
    id: str
    to: str  # "owner" or "wife"
    kind: str
    params: dict = field(default_factory=dict)  # words come from notifier.messages


@dataclass
class Situation:
    now: datetime
    phase: str  # unarmed, normal, warning, grace, wife_window, decided, final
    holds: list[str]
    hold_until: datetime | None
    armed: bool
    owner_deadline: datetime | None = None
    grace_clock_start: datetime | None = None
    grace_end: datetime | None = None
    wife_window_end: datetime | None = None
    w_selected_at: datetime | None = None
    decision: Decision | None = None
    decision_due: str | None = None
    period: str | None = None  # "owner_grace" or "wife_window"
    period_end: datetime | None = None
    alerts: list[Alert] = field(default_factory=list)

    @property
    def may_act(self) -> bool:
        return not self.holds

    def hold_remaining(self) -> int:
        if self.hold_until is None or self.hold_until <= self.now:
            return 0
        return int((self.hold_until - self.now).total_seconds())

    def time_left(self) -> int | None:
        if self.period_end is None:
            return None
        return max(0, int((self.period_end - self.now).total_seconds()))


def advance(t0: datetime, duration: timedelta, pauses: list[Pause]) -> datetime:
    """The instant at which `duration` of running time has elapsed since t0.

    Downtime recorded in `pauses` does not count as elapsed time, so every
    pause that begins before the computed instant pushes it out.

    Overlapping ledger entries count once: a tick that dies after recording a
    pause leaves last_tick behind, so later ticks record the same downtime
    again from the same start.
    """
    merged: list[list[datetime]] = []
    for start, end in sorted((p.start, p.end) for p in pauses if p.start is not None):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    t = t0 + duration
    for start, end in merged:
        if end <= t0:
            continue
        if start >= t:
            break
        t += end - max(start, t0)
    return t


# Once a decision exists only these alerts are raised or sent; anything else
# still pending then counts as missed. After final the failure alerts end too
# (tracking has stopped), so only the one-off messages remain.
AFTER_DECISION = frozenset({"decision", "false_triggered", "final", "broadcast",
                            "broadcast_failed", "rebroadcast_failed"})
AFTER_FINAL = frozenset({"decision", "false_triggered", "final", "broadcast"})


def allowed_kinds(facts: Facts) -> frozenset | None:
    """The alert kinds that may still go out, or None when all may."""
    if facts.final_at is not None:
        return AFTER_FINAL
    if facts.decision is not None:
        return AFTER_DECISION
    return None


def _quiet(alerts: list[Alert], facts: Facts) -> list[Alert]:
    allowed = allowed_kinds(facts)
    return alerts if allowed is None else [a for a in alerts if a.kind in allowed]


def grace_alert_id(owner_deadline: datetime) -> str:
    return f"grace-wife:{fmt_ts(owner_deadline)}:0"


WEEK = 7 * DAY


def _day_index(start: datetime, now: datetime) -> int:
    return int((now - start) // DAY)


def derive(timing: Timing, facts: Facts, now: datetime, faults: list[str] | tuple = ()) -> Situation:
    holds = list(faults)
    hold_until = None
    for p in facts.pauses:
        if p.hold_until and p.hold_until > now and (hold_until is None or p.hold_until > hold_until):
            hold_until = p.hold_until
    if hold_until is not None:
        holds.append("cold_start_hold")
    if facts.last_tick is not None and facts.last_tick > now:
        holds.append("clock_went_backwards")

    armed = facts.armed_at is not None and facts.owner_checkin is not None
    if not armed:
        holds.append("unarmed")
        s = Situation(now=now, phase="unarmed", holds=holds, hold_until=hold_until, armed=False)
        s.alerts = _quiet(_alerts(s, timing, facts), facts)
        return s

    for role, t in (("owner", facts.owner_checkin), ("wife", facts.wife_checkin)):
        if t is not None and t > now + timing.max_clock_skew:
            holds.append(f"{role}_checkin_in_future")

    pauses = facts.pauses
    owner_deadline = advance(facts.owner_checkin, timing.owner_period, pauses)

    # Grace clock: starts when the first grace alert to the wife is accepted by
    # the notifier backend, or after the maximum wait, whichever is earlier.
    wait_bound = advance(owner_deadline, timing.max_alert_wait, pauses)
    accepted = facts.alert_accepted.get(grace_alert_id(owner_deadline))
    grace_clock_start = min(accepted, wait_bound) if accepted else wait_bound
    grace_end = advance(grace_clock_start, timing.owner_grace, pauses)
    wife_window_end = advance(grace_end, timing.wife_window, pauses)

    decision = facts.decision
    w_selected = None
    if facts.wife_checkin is not None and facts.wife_checkin >= owner_deadline:
        if decision is None or facts.wife_checkin < decision.at:
            w_selected = facts.wife_checkin

    s = Situation(
        now=now, phase="normal", holds=holds, hold_until=hold_until, armed=True,
        owner_deadline=owner_deadline,
        grace_clock_start=grace_clock_start, grace_end=grace_end,
        wife_window_end=wife_window_end, w_selected_at=w_selected, decision=decision,
    )

    if facts.final_at is not None:
        s.phase = "final"
    elif decision is not None:
        s.phase = "decided"
    elif now < owner_deadline:
        s.phase = "warning" if now >= owner_deadline - timing.warning_window else "normal"
    elif now < grace_end:
        s.phase = "grace"
        s.period, s.period_end = "owner_grace", grace_end
    else:
        s.phase = "wife_window"
        s.period, s.period_end = "wife_window", wife_window_end
        if w_selected is not None:
            s.decision_due = "W"
        elif now >= wife_window_end:
            s.decision_due = "F"

    s.alerts = _quiet(_alerts(s, timing, facts), facts)
    return s


def _alerts(s: Situation, timing: Timing, facts: Facts) -> list[Alert]:
    """The alerts that should exist now. Ids of state-change alerts are keyed
    on stable facts (check-in or decision times), not on derived deadlines,
    because a pause moves derived deadlines."""
    out: list[Alert] = []
    now = s.now
    today = now.date().isoformat()

    for reason in s.holds:
        if reason == "cold_start_hold":
            out.append(Alert(f"hold:{fmt_ts(s.hold_until)}", "owner", "hold", {"until": s.hold_until}))
        elif reason == "unarmed":
            out.append(Alert(f"unarmed:{today}", "owner", "unarmed"))
        else:
            out.append(Alert(f"fault:{reason}:{today}", "owner", "fault", {"reason": reason}))
    out.extend(_cleared(s, facts))

    if not s.armed:
        return out
    od = s.owner_deadline
    left = od - now
    decision = s.decision

    # State changes for the owner. "armed" is also the end of the unarmed hold.
    # Check-ins after a decision change nothing and are not reported
    # (_quiet drops them anyway; the conditions keep the params honest).
    out.append(Alert(f"armed:{fmt_ts(facts.armed_at)}", "owner", "armed", {"deadline": od}))
    if facts.owner_checkin != facts.armed_at and (decision is None or facts.owner_checkin < decision.at):
        out.append(Alert(f"checkin-owner:{fmt_ts(facts.owner_checkin)}", "owner", "checkin_owner", {
            "at": facts.owner_checkin, "deadline": od}))
    w = facts.wife_checkin
    if w is not None and (decision is None or w <= decision.at):
        params = {"at": w}
        if w < od:
            params["effect"] = "before_deadline"
        else:
            params["effect"] = "selects_w"
            params["decision_at"] = decision.at if decision is not None else max(w, s.grace_end)
        out.append(Alert(f"checkin-wife:{fmt_ts(w)}", "owner", "checkin_wife", params))

    # Weekly reminder before the warning window, counted back from the
    # deadline: boundary b_k = od - k weeks. Only a boundary crossed after
    # the check-in counts, so a check-in is not followed by a reminder at once.
    if s.phase == "normal":
        k = -((now - od) // WEEK)  # smallest k with b_k <= now
        if k >= 1 and od - k * WEEK > facts.owner_checkin:
            out.append(Alert(f"owner-weekly:{fmt_ts(facts.owner_checkin)}:{k}", "owner", "weekly",
                             {"deadline": od, "days_left": left // DAY}))

    # The wife is not involved before the owner deadline: no alerts to her.
    if s.phase == "warning":
        n = _day_index(od - timing.warning_window, now)
        out.append(Alert(f"owner-warning:{fmt_ts(od)}:{n}", "owner", "warning",
                         {"deadline": od, "days_left": left // DAY}))

    # Not in the tick that records the decision: "please check in" followed
    # at once by "the transfer has started" would only confuse.
    if s.phase in ("grace", "wife_window") and not (s.decision_due and s.may_act):
        n = _day_index(od, now)
        params = {"phase": s.phase, "deadline": od, "wife_window_end": s.wife_window_end}
        out.append(Alert(f"grace-wife:{fmt_ts(od)}:{n}", "wife", "grace",
                         {**params, "checked_in": s.w_selected_at is not None}))
        out.append(Alert(f"grace-owner:{fmt_ts(od)}:{n}", "owner", "grace", params))
    if s.phase == "wife_window":
        out.append(Alert(f"wife-window:{fmt_ts(facts.owner_checkin)}", "owner", "wife_window",
                         {"wife_window_end": s.wife_window_end}))

    if decision is not None:
        for to in ("owner", "wife"):
            out.append(Alert(f"decision:{fmt_ts(decision.at)}:{to}", to, "decision",
                             {"at": decision.at, "choice": decision.choice}))
        if decision.choice == "F" and facts.wife_checkin is not None and facts.wife_checkin > decision.at:
            out.append(Alert(f"false-triggered:{fmt_ts(facts.wife_checkin)}", "wife", "false_triggered"))
    if facts.final_at is not None and decision is not None:
        for to in ("owner", "wife"):
            out.append(Alert(f"final:{fmt_ts(facts.final_at)}:{to}", to, "final", {"choice": decision.choice}))
    return out


def _cleared(s: Situation, facts: Facts) -> list[Alert]:
    """"Hold cleared" alerts, derived from the hold alerts already on disk
    (their ids are in facts.alert_accepted), so no extra state is kept.

    A fault that clears, returns and clears again on the same day gives one
    cleared alert: at most one enforced/cleared pair per reason and day.
    """
    out: list[Alert] = []
    known = facts.alert_accepted
    ends = [p.hold_until for p in facts.pauses if p.hold_until is not None]
    if ends:
        h = max(ends)
        if h <= s.now and "cold_start_hold" not in s.holds and f"hold:{fmt_ts(h)}" in known:
            out.append(Alert(f"hold-cleared:{fmt_ts(h)}", "owner", "hold_cleared", {"until": h}))
    newest: dict[str, str] = {}
    for alert_id in known:
        if alert_id.startswith("fault:"):
            reason, _, date = alert_id[len("fault:"):].rpartition(":")
            if reason and date > newest.get(reason, ""):
                newest[reason] = date
    for reason, date in sorted(newest.items()):
        if reason not in s.holds:
            out.append(Alert(f"fault-cleared:{reason}:{date}", "owner", "fault_cleared", {"reason": reason}))
    return out

# ---- per-role views -------------------------------------------------------

def _iso(dt: datetime | None) -> str | None:
    return fmt_ts(dt) if dt else None


def _common(s: Situation) -> dict:
    return {
        "period": s.period,
        "period_end": _iso(s.period_end),
        "time_left_seconds": s.time_left(),
        "hold_remaining_seconds": s.hold_remaining(),
    }


def owner_view(s: Situation, facts: Facts) -> dict:
    return {
        "state": s.phase,
        "holds": list(s.holds),
        "hold_until": _iso(s.hold_until),
        "armed_at": _iso(facts.armed_at),
        "owner_checkin": _iso(facts.owner_checkin),
        "wife_checkin": _iso(facts.wife_checkin),
        "owner_deadline": _iso(s.owner_deadline),
        "grace_clock_start": _iso(s.grace_clock_start),
        "grace_end": _iso(s.grace_end),
        "wife_window_end": _iso(s.wife_window_end),
        "w_selected_at": _iso(s.w_selected_at),
        "decision": {"choice": s.decision.choice, "at": fmt_ts(s.decision.at)} if s.decision else None,
        "decision_due": s.decision_due,
        "final_at": _iso(facts.final_at),
        "pauses": len(facts.pauses),
        **_common(s),
    }


def wife_view(s: Situation, facts: Facts) -> dict:
    # She only has a deadline from the owner deadline until the decision:
    # the end of the wife window, after which pair F is decided.
    # checked_in_at: her check-in once it counts (at or after the owner
    # deadline); an earlier one is stored but ignored, so it is not shown.
    deadline = None
    checked_in_at = None
    if s.armed and facts.wife_checkin is not None and facts.wife_checkin >= s.owner_deadline:
        checked_in_at = facts.wife_checkin
    if s.decision is not None:
        if s.decision.choice == "W":
            state = "inheritance"
        elif facts.wife_checkin is not None and facts.wife_checkin > s.decision.at:
            state = "false_triggered"
        else:
            state = "closed"
    elif s.armed and s.now >= s.owner_deadline:
        state = "grace" if s.now < s.grace_end else "wife_window"
        deadline = s.wife_window_end
    else:
        state = "normal"
    return {
        "state": state,
        "deadline": _iso(deadline),
        "checked_in_at": _iso(checked_in_at),
        **_common(s),
    }


def family_view(s: Situation, facts: Facts) -> dict:
    if s.decision is not None:
        state = "inheritance"
    elif s.armed and s.now >= s.owner_deadline:
        state = "alert"
    else:
        state = "standby"
    return {"state": state, **_common(s)}

