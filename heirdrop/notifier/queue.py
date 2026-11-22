"""Pure helpers over the alert queue (the alert files as dicts). Nothing here
writes: which alerts are worth sending is recomputed each time from the files.
"""
from __future__ import annotations

# Kinds raised again and again (daily or weekly). After an outage only the
# newest alert of a series is worth sending; the older ones are "missed".
REPEATING = frozenset({"warning", "weekly", "grace", "fault", "unarmed", "tx_problem",
                       "broadcast_failed", "rebroadcast_failed", "channel_broken"})
# Reminders about "the current deadline": a newer one replaces an older one
# even when the deadline (part of the id) has moved since.
BY_KIND = frozenset({"warning", "weekly", "grace"})


def accepted_at(alert: dict) -> str | None:
    """When the backend accepted it; `delivered_at` is the name before the notifier."""
    if "accepted_at" in alert:
        return alert["accepted_at"]
    return alert.get("delivered_at")


def series(alert: dict) -> tuple:
    kind = alert.get("kind")
    if kind in BY_KIND:
        return (alert.get("to"), kind)
    if kind in REPEATING:
        return (alert.get("to"), kind, str(alert["id"]).rsplit(":", 1)[0])
    return (alert.get("to"), kind, alert["id"])  # a series of one


def _order(alert: dict) -> tuple:
    return (str(alert.get("created_at") or ""), str(alert["id"]))


def sendable(alerts: list[dict], allowed: frozenset | None = None) -> tuple[list[dict], list[dict]]:
    """Split the pending ones among all alerts into (to_send, missed).

    A pending alert is missed when a newer alert of its series exists,
    accepted or not; so once the newest is sent, the older ones stay missed.
    With `allowed` (logic.allowed_kinds after a decision) a pending alert of
    any other kind is missed too: a reminder from before the decision must
    not arrive after it. to_send is in creation order.
    """
    newest: dict[tuple, dict] = {}
    for a in alerts:
        key = series(a)
        if key not in newest or _order(a) > _order(newest[key]):
            newest[key] = a
    keep = {id(a) for a in newest.values() if allowed is None or a.get("kind") in allowed}
    pending = [a for a in alerts if not accepted_at(a)]
    to_send = sorted((a for a in pending if id(a) in keep), key=_order)
    missed = sorted((a for a in pending if id(a) not in keep), key=_order)
    return to_send, missed


def interleave(alerts: list[dict]) -> list[dict]:
    """Sending order: owner and wife take turns, each in creation order, so a
    backlog for one cannot hold back the other (the wife's first grace alert
    starts the grace clock)."""
    seen: dict[str, int] = {}
    ranked = []
    for a in alerts:
        n = seen.get(a.get("to"), 0)
        seen[a.get("to")] = n + 1
        ranked.append((n, a.get("to") != "wife", _order(a), a))
    return [r[-1] for r in sorted(ranked, key=lambda r: r[:3])]


def summary(alerts: list[dict], allowed: frozenset | None = None) -> dict:
    """Per recipient: the last accepted alert and how many are waiting."""
    to_send, _ = sendable(alerts, allowed)
    out = {}
    for to in ("owner", "wife"):
        done = [a for a in alerts if a.get("to") == to and accepted_at(a)]
        last = max(done, key=lambda a: (str(accepted_at(a)), str(a["id"])), default=None)
        out[to] = {
            "last_notified_on": accepted_at(last) if last else None,
            "last_kind": last.get("kind") if last else None,
            "last_id": last["id"] if last else None,
            "pending": sum(1 for a in to_send if a.get("to") == to),
        }
    return out
