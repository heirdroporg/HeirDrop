"""Alert wording. `render(kind, to, params)` turns an alert into the text that
is stored in the alert file and sent.

Rules for every text: English, plain text (no markup), and no txids,
addresses or amounts; details stay behind `heirdrop health`. An unknown
(kind, to) raises KeyError, so a new alert kind cannot go out without words.
"""
from __future__ import annotations

from datetime import datetime

from ..clock import UTC

HEALTH = "Details: heirdrop health."

FAULTS = {
    "node_unreachable": "bitcoind does not answer or answers nonsense",
    "node_not_synced": "bitcoind is not synced",
    "clock_skew": "the system clock disagrees with the block chain",
    "clock_went_backwards": "the system clock went backwards",
    "owner_checkin_in_future": "the owner check-in lies in the future",
    "wife_checkin_in_future": "the wife check-in lies in the future",
    "state_files": "the state files are unreadable or inconsistent",
    "tx_checksum": "a transaction file is missing or changed",
    "tx_decode": "a transaction does not decode",
    "tx_address": "a transaction pays an unexpected address",
}


def when(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def fault_text(reason: str) -> str:
    """Human words for a hold reason; the code stays in brackets for health."""
    base, _, tx = reason.partition(":")
    words = FAULTS.get(base, "an unexpected problem")
    return f"{words}{f' ({tx})' if tx else ''} [{reason}]"


def _days(n: int) -> str:
    return "1 day" if n == 1 else f"{n} days"


# ---- owner ------------------------------------------------------------------

def _hold(p):
    return (f"Heirdrop: cold start hold until {when(p['until'])}. The service was down or has just "
            f"started; nothing is decided or broadcast until then.")


def _hold_cleared(p):
    return f"Heirdrop: the cold start hold of {when(p['until'])} has ended."


def _unarmed(p):
    return "Heirdrop is not armed. Check in as owner to arm it."


def _fault(p):
    return (f"Heirdrop is holding: {fault_text(p['reason'])}. Nothing is decided or broadcast "
            f"until this clears. {HEALTH}")


def _fault_cleared(p):
    return f"Heirdrop: hold cleared: {fault_text(p['reason'])}."


def _armed(p):
    return f"Heirdrop is armed. Next check-in deadline {when(p['deadline'])}."


def _checkin_owner(p):
    return f"Owner check-in recorded at {when(p['at'])}. Next deadline {when(p['deadline'])}."


def _checkin_wife(p):
    effect = p["effect"]
    at = when(p["at"])
    if effect == "selects_w":
        return (f"Your wife checked in at {at}. Unless you check in, pair W (to her) "
                f"is decided at {when(p['decision_at'])}.")
    return f"Your wife checked in at {at}, before your deadline. It has no effect."


def _weekly(p):
    return (f"Reminder: {_days(p['days_left'])} left until your check-in deadline "
            f"{when(p['deadline'])}.")


def _warning(p):
    return (f"Your check-in deadline {when(p['deadline'])} is near ({_days(p['days_left'])} left). "
            f"Please check in.")


def _grace_owner(p):
    if p["phase"] == "wife_window":
        return (f"Your deadline {when(p['deadline'])} has passed and the grace period is over. "
                f"Check in to cancel. If your wife checks in, pair W is decided; "
                f"otherwise pair F at {when(p['wife_window_end'])}.")
    return (f"Your deadline {when(p['deadline'])} has passed; your wife is notified daily. "
            f"Check in to cancel. Otherwise a decision follows, at the latest at "
            f"{when(p['wife_window_end'])}.")


def _wife_window(p):
    return (f"The grace period ended without your check-in. The wife window is open until "
            f"{when(p['wife_window_end'])}. Check in to cancel.")


def _decision_owner(p):
    return f"Decision recorded at {when(p['at'])}: pair {p['choice']}. Broadcasting follows. {HEALTH}"


def _final_owner(p):
    return f"The transactions of pair {p['choice']} are confirmed. The case is final."


def _tx_problem(p):
    return f"Validation found a problem with transaction {p['name']}. No hold. {HEALTH}"


def _broadcast(p):
    if p["dry_run"]:
        return f"Dry run: transaction {p['name']} would have been broadcast now."
    return f"Transaction {p['name']} was broadcast. {HEALTH}"


def _broadcast_failed(p):
    return f"Cannot broadcast transaction {p['name']}; retrying every tick. {HEALTH}"


def _rebroadcast_failed(p):
    return f"Rebroadcast of transaction {p['name']} was rejected; retrying. {HEALTH}"


def _channel_broken(p):
    return (f"Telegram does not accept messages to your wife ({p['error']}). She may have "
            f"blocked the bot or never pressed Start. The service keeps retrying.")


# ---- wife -------------------------------------------------------------------

def _grace_wife(p):
    if p.get("checked_in"):
        return ("Your check-in is recorded. There is nothing more to do; you will get a message "
                "when the decision is made.")
    if p["phase"] == "wife_window":
        return (f"The owner has still not checked in. Please check in now in the app. "
                f"If you do not check in by {when(p['wife_window_end'])}, the family transfer is started.")
    return ("The owner has not checked in. Please check in now in the app to confirm you are reachable. "
            f"Last day to check in: {when(p['wife_window_end'])}.")


def _decision_wife(p):
    if p["choice"] == "W":
        return "The transfer to you has been started. Open the app for your package."
    return "The waiting time ended without your check-in. The family transfer has been started."


def _false_triggered(p):
    return "You checked in after the family transfer was started. Contact the family member."


def _final_wife(p):
    return "The transfer is confirmed and final."


def _test(p):
    return f"Test message from the heirdrop service, sent at {when(p['at'])}. No action needed."


TEXTS = {
    ("test", "owner"): _test,
    ("test", "wife"): _test,
    ("hold", "owner"): _hold,
    ("hold_cleared", "owner"): _hold_cleared,
    ("unarmed", "owner"): _unarmed,
    ("fault", "owner"): _fault,
    ("fault_cleared", "owner"): _fault_cleared,
    ("armed", "owner"): _armed,
    ("checkin_owner", "owner"): _checkin_owner,
    ("checkin_wife", "owner"): _checkin_wife,
    ("weekly", "owner"): _weekly,
    ("warning", "owner"): _warning,
    ("grace", "owner"): _grace_owner,
    ("wife_window", "owner"): _wife_window,
    ("decision", "owner"): _decision_owner,
    ("final", "owner"): _final_owner,
    ("tx_problem", "owner"): _tx_problem,
    ("broadcast", "owner"): _broadcast,
    ("broadcast_failed", "owner"): _broadcast_failed,
    ("rebroadcast_failed", "owner"): _rebroadcast_failed,
    ("channel_broken", "owner"): _channel_broken,
    ("grace", "wife"): _grace_wife,
    ("decision", "wife"): _decision_wife,
    ("false_triggered", "wife"): _false_triggered,
    ("final", "wife"): _final_wife,
}


def render(kind: str, to: str, params: dict) -> str:
    return TEXTS[(kind, to)](params)
